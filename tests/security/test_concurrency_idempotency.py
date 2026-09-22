"""B6 / B7：会话级并发锁与 API 层幂等。

B6  同一会话并发两条消息 → 第二条 409 CONFLICT + Retry-After。
B7  相同 Idempotency-Key 发两次 → 第二次直接返回第一次的结果，
    不再调用模型（fake 计数 = 1）、不产生第二条消息。

按设计文档 §16，集成测试用 fake LLM，不真调模型：
    通过 FastAPI dependency_overrides 注入 FakeLLM，
    其 create() 在 gate 事件上阻塞，用于让第一条请求「持有锁不放」。
"""

import asyncio
import json
import uuid

import pytest

from apps.api.api.v1.chat import get_llm_client
from tests.security.conftest import (
    auth_header,
    insert_conversation_for,
    register_and_login,
)


class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Usage:
    def __init__(self):
        self.prompt_tokens = 5
        self.completion_tokens = 3
        self.total_tokens = 8


class _Chunk:
    def __init__(self, content=None):
        self.choices = [] if content is None else [_Choice(content)]
        self.usage = None


class _Completions:
    """mimics openai AsyncOpenAI().chat.completions 的最小接口。"""

    def __init__(self, outer: "FakeLLM"):
        self._outer = outer

    async def create(self, **kwargs):
        self._outer.calls += 1

        async def _gen():
            if self._outer.gate is not None:
                await asyncio.wait_for(self._outer.gate.wait(), timeout=10)
            for piece in ["你好", "，", "世界"]:
                yield _Chunk(piece)
            final = _Chunk(None)
            final.usage = _Usage()
            yield final

        return _gen()


class FakeLLM:
    """gate 非 None 时把请求卡在流中（用于让第一条请求持有会话锁）。"""

    def __init__(self):
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions(self)


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        event = "message"
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip(" "))
        if data_lines:
            events.append((event, json.loads("\n".join(data_lines))))
    return events


@pytest.mark.asyncio
async def test_b6_concurrent_same_conversation_conflicts(client, app, worker_engine):
    """B6：同一会话并发两条消息 → 第二条 409 CONFLICT + Retry-After。"""
    fake = FakeLLM()
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        tokens = await register_and_login(client)
        conv_id = await insert_conversation_for(worker_engine, tokens["user_id"])
        headers = {**auth_header(tokens), "Content-Type": "application/json"}

        fake.gate = asyncio.Event()

        async def send_once(key: str):
            return await client.post(
                "/api/v1/chat",
                json={"content": "数到十", "conversation_id": conv_id},
                headers={**headers, "Idempotency-Key": key},
            )

        task1 = asyncio.create_task(send_once(f"b6-{uuid.uuid4().hex}"))
        # 等 task1 拿到锁并进入模型调用（轮询 fake.calls）
        for _ in range(100):
            if fake.calls >= 1:
                break
            await asyncio.sleep(0.05)
        assert fake.calls == 1, "第一条请求未进入模型调用，锁场景不成立"

        response2 = await send_once(f"b6-{uuid.uuid4().hex}")
        assert response2.status_code == 409, response2.text
        body = response2.json()
        assert body["error"]["code"] == "CONFLICT"
        assert "Retry-After" in response2.headers

        # 放行第一条，等待其正常完成（锁释放）
        fake.gate.set()
        response1 = await task1
        assert response1.status_code == 200, response1.text
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


@pytest.mark.asyncio
async def test_b7_idempotency_key_replays_without_side_effects(client, app, worker_engine):
    """B7：相同 Idempotency-Key 发两次 → 第二次重放首次结果，模型只调一次。"""
    fake = FakeLLM()
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        tokens = await register_and_login(client)
        conv_id = await insert_conversation_for(worker_engine, tokens["user_id"])
        headers = {**auth_header(tokens), "Content-Type": "application/json"}
        key = f"b7-{uuid.uuid4().hex}"

        r1 = await client.post(
            "/api/v1/chat",
            json={"content": "用一句话解释什么是 RAG", "conversation_id": conv_id},
            headers={**headers, "Idempotency-Key": key},
        )
        assert r1.status_code == 200, r1.text
        events1 = _parse_sse(r1.text)
        done1 = [d for e, d in events1 if e == "done"][0]
        assert done1["replayed"] is False

        # 相同 key 再发：重放（不调模型）
        r2 = await client.post(
            "/api/v1/chat",
            json={"content": "用一句话解释什么是 RAG", "conversation_id": conv_id},
            headers={**headers, "Idempotency-Key": key},
        )
        assert r2.status_code == 200, r2.text
        assert r2.headers.get("X-Idempotent-Replay") == "true"
        events2 = _parse_sse(r2.text)
        done2 = [d for e, d in events2 if e == "done"][0]
        assert done2["replayed"] is True

        # 内容一致
        text1 = "".join(d.get("delta", "") for e, d in events1 if e == "token")
        text2 = "".join(d.get("delta", "") for e, d in events2 if e == "token")
        assert text1 == text2 == "你好，世界"

        # 关键断言：模型只被调用一次
        assert fake.calls == 1, f"模型被调用了 {fake.calls} 次"

        # 用不同 key 再发一次：正常执行（不受上次 key 影响）
        r3 = await client.post(
            "/api/v1/chat",
            json={"content": "再来一条", "conversation_id": conv_id},
            headers={**headers, "Idempotency-Key": f"b7-{uuid.uuid4().hex}"},
        )
        assert r3.status_code == 200
        assert fake.calls == 2
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
