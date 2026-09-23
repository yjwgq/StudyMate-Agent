"""幂等重放的行为契约（M3 补齐）。

M3 验收实锤的缺陷：幂等重放只回放正文，**丢掉了引用面板**——
用户重试同一请求时会看到「答案里有 [1] 但没有来源可点」。
修复 = 终态缓存带上 citations，重放时先发 citations 事件再回放正文。
本测试锁死该行为（B7 的集成测试用 fake LLM，不覆盖检索/citations）。
"""

from typing import Any

from apps.api.api.v1.chat import _replay_error_stream, _replay_stream

CITATIONS = [
    {
        "n": 1,
        "chunk_id": "chunk-1",
        "parent_chunk_id": "parent-1",
        "document_id": "doc-1",
        "title": "ml_basics.md",
        "page": None,
        "snippet": "监督学习需要带标签的训练数据。",
        "score": 0.9,
        "rerank_score": None,
    }
]


async def _collect(gen: Any) -> list[str]:
    return [chunk async for chunk in gen]


def _events(chunks: list[str]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for block in "".join(chunks).split("\n\n"):
        if not block.strip():
            continue
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                import json

                data = json.loads(line[len("data:"):].lstrip())
        if event:
            out.append((event, data))
    return out


class TestReplayStream:
    async def test_replays_citations_before_content(self) -> None:
        """引用面板必须先于正文下发（前端先渲染脚注再等 token）。"""
        payload = {"content": "监督学习需要标签[1]。", "conversation_id": "c-1", "citations": CITATIONS}
        events = _events(await _collect(_replay_stream(payload, "t" * 32)))

        assert events[0][0] == "citations"
        assert events[0][1]["citations"] == CITATIONS
        assert events[1][0] == "token"
        assert events[1][1]["delta"] == "监督学习需要标签[1]。"
        assert events[2][0] == "done"
        # done 里也带 citation_count，便于前端/评测核对
        assert events[2][1]["citation_count"] == 1
        assert events[2][1]["replayed"] is True

    async def test_no_citations_event_when_absent(self) -> None:
        """老缓存（修复前写入的终态）没有 citations 字段 → 不发空事件。"""
        payload = {"content": "没有资料的回答", "conversation_id": "c-2"}
        events = _events(await _collect(_replay_stream(payload, "t" * 32)))
        assert [e for e, _ in events] == ["token", "done"]
        assert events[1][1]["citation_count"] == 0

    async def test_replay_error_stream(self) -> None:
        events = _events(
            await _collect(_replay_error_stream({"code": "LLM_OFFLINE", "message": "上游不可用"}, "t" * 32))
        )
        assert events[0][0] == "error"
        assert events[0][1]["code"] == "LLM_OFFLINE"
        assert events[1][0] == "done"
