"""POST /api/v1/chat —— M1 版本。

相对 M0 的演进（本里程碑落地）：
    - 认证：所有请求必须携带 Bearer access token（401 走统一错误格式）；
    - 会话：带 conversation_id 时校验归属（他人的 → 404，B4）；
      不带时自动创建新会话（done 事件与 X-Conversation-Id 头返回 id）；
    - 并发：同一会话串行独占（§7.4）—— Redis 锁 SET NX PX 300s，
      获取失败 → 409 CONFLICT + Retry-After（B6）；
    - 幂等：支持 Idempotency-Key（§7.6）—— 已完成的相同 key 直接重放，
      不再调用模型、不再落库（B7）；进行中 → 409；
    - 落库：用户消息与助手消息（含 seq / token_usage）写 messages 表，
      UNIQUE(conversation_id, seq) 由 DB 兜底。

后续演进（M5）：改为「投递任务 + 订阅流」，SSE 支持 Last-Event-ID 重连续传。
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, Field

from apps.api.core import idempotency
from apps.api.core.config import settings
from apps.api.core.db import tenant_session
from apps.api.core.deps import UserCtx, current_user
from apps.api.core.errors import AppError, ErrorCode, new_trace_id
from apps.api.core.locks import acquire_conversation_lock, release_conversation_lock
from apps.api.core.redis import get_redis
from apps.api.repositories import conversations as conv_repo
from apps.api.sse import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_TOKEN,
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    sse_event,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

SYSTEM_PROMPT = "你是一个乐于助人的中文 AI 助理。回答简洁准确，不确定时如实说明。"


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000, description="用户输入")
    conversation_id: UUID | None = Field(
        default=None,
        description="会话 ID。缺省时自动创建新会话（done 事件返回新 id）。",
    )


async def get_llm_client() -> AsyncOpenAI:
    """LLM 客户端工厂。

    独立成依赖是为了可测试：集成测试用 dependency_overrides 注入 fake，
    不真调模型（设计文档 §16「集成测试用 fake LLM」）。
    """
    if not settings.llm_configured:
        raise AppError(
            ErrorCode.LLM_OFFLINE,
            "LLM 未配置：请在 .env 中填写 LLM_API_KEY 与 LLM_MODEL",
            503,
        )
    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout_s,
    )


async def _model_stream(
    client: AsyncOpenAI, content: str
) -> AsyncIterator[tuple[str | None, Any]]:
    """向模型发起流式请求，逐块产出 (delta, chunk)。

    异常向上抛出，由 chat() 的生成器统一转成 error 事件 ——
    HTTP 状态码在流开始时就已发出，中途断流前端只会看到「连接意外关闭」，
    拿不到任何可读信息（M0 验收 A4 的教训）。
    """
    stream = await client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        stream=True,
        # 流末尾补 usage 分片，用于 token 统计（A6）。
        # 若服务商不认该参数返回 400，删掉本行即可（token 统计随之失效）。
        stream_options={"include_usage": True},
    )
    async for chunk in stream:
        delta = None
        if chunk.choices:
            delta = chunk.choices[0].delta.content
        yield delta, chunk


@router.post("/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    user: Annotated[UserCtx, Depends(current_user)],
    llm: Annotated[AsyncOpenAI, Depends(get_llm_client)],
    idem_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
) -> StreamingResponse:
    trace_id = getattr(request.state, "trace_id", "") or new_trace_id()
    redis = get_redis()

    # ---- 1) 幂等：只读查终态，已完成 / 已失败 → 直接重放（不调模型、不落库，B7）----
    if idem_key:
        status, payload = await idempotency.peek(redis, str(user.id), idem_key)
        if status == "completed":
            return StreamingResponse(
                _replay_stream(payload, trace_id),
                media_type=SSE_MEDIA_TYPE,
                headers={**SSE_HEADERS, "X-Idempotent-Replay": "true"},
            )
        if status == "failed":
            err = (payload or {}).get("error") or {"code": "INTERNAL", "message": "上次请求失败"}
            return StreamingResponse(
                _replay_error_stream(err, trace_id),
                media_type=SSE_MEDIA_TYPE,
                headers=SSE_HEADERS,
            )
        # absent / in_progress → 继续正常流程；
        # 并发同 key 的 409 由第 4 步（锁内 acquire）统一判定

    # ---- 2) 会话归属 / 创建 ----
    if req.conversation_id is not None:
        async with tenant_session(user.id) as session:
            conv = await conv_repo.get_conversation(session, user.id, req.conversation_id)
        if conv is None:
            # RLS + 仓储过滤后查不到：不区分「不存在」与「不属于你」（B4）
            raise AppError(ErrorCode.NOT_FOUND, "会话不存在", 404)
        conversation_id = str(req.conversation_id)
    else:
        async with tenant_session(user.id) as session:
            conv = await conv_repo.create_conversation(
                session, user.id, title=req.content[:60]
            )
        conversation_id = str(conv["id"])
    cid = UUID(conversation_id)

    # ---- 3) 会话级锁（§7.4，B6）：串行独占 ----
    lock_token = await acquire_conversation_lock(redis, conversation_id)
    if lock_token is None:
        raise AppError(
            ErrorCode.CONFLICT,
            "该会话正在处理中，请稍后再试",
            409,
            headers={"Retry-After": "5"},
        )

    # ---- 4) 幂等占位（拿到锁之后再占位，避免锁冲突污染幂等状态）----
    if idem_key:
        status, payload = await idempotency.acquire(redis, str(user.id), idem_key)
        if status != "acquired":
            await release_conversation_lock(redis, str(conversation_id), lock_token)
            if status == "completed":
                return StreamingResponse(
                    _replay_stream(payload, trace_id),
                    media_type=SSE_MEDIA_TYPE,
                    headers={**SSE_HEADERS, "X-Idempotent-Replay": "true"},
                )
            raise AppError(
                ErrorCode.CONFLICT, "相同 Idempotency-Key 的请求正在处理中", 409,
                headers={"Retry-After": "3"},
            )

    # ---- 5) 用户消息落库（顺序号在锁内分配，UNIQUE(cid, seq) DB 兜底）----
    async with tenant_session(user.id) as session:
        seq = await conv_repo.next_seq(session, cid)
        await conv_repo.insert_message(
            session,
            conversation_id=cid,
            user_id=user.id,
            seq=seq,
            role="user",
            content=req.content,
            status="completed",
            trace_id=trace_id,
        )

    return StreamingResponse(
        _chat_stream(
            llm=llm,
            redis=redis,
            user=user,
            conversation_id=cid,
            content=req.content,
            user_seq=seq,
            idem_key=idem_key,
            lock_token=lock_token,
            trace_id=trace_id,
        ),
        media_type=SSE_MEDIA_TYPE,
        headers={**SSE_HEADERS, "X-Conversation-Id": conversation_id},
    )


async def _chat_stream(
    *,
    llm: AsyncOpenAI,
    redis,
    user: UserCtx,
    conversation_id: UUID,
    content: str,
    user_seq: int,
    idem_key: str | None,
    lock_token: str,
    trace_id: str,
) -> AsyncIterator[str]:
    """执行流式回复：转发增量 → 落库 → 幂等终态 → 释放锁。

    所有异常都转成 error 事件而不是断流；锁在任何路径下都会释放。
    """
    started = time.perf_counter()
    full_text_parts: list[str] = []
    usage = None
    final_status = "completed"

    try:
        async for delta, chunk in _model_stream(llm, content):
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if delta:
                full_text_parts.append(delta)
                yield sse_event(EVENT_TOKEN, {"delta": delta})

        elapsed = time.perf_counter() - started
        full_text = "".join(full_text_parts)
        usage_dict = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        } if usage else None

        # 助手消息落库 + 会话活跃时间
        async with tenant_session(user.id) as session:
            assistant = await conv_repo.insert_message(
                session,
                conversation_id=conversation_id,
                user_id=user.id,
                seq=user_seq + 1,
                role="assistant",
                content=full_text,
                status="completed",
                token_usage=usage_dict,
                trace_id=trace_id,
            )
            await conv_repo.touch_last_message(session, conversation_id)

        yield sse_event(
            EVENT_DONE,
            {
                "finish_reason": "stop",
                "conversation_id": str(conversation_id),
                "message_id": str(assistant["id"]),
                "elapsed_s": round(elapsed, 2),
                "usage": usage_dict,
                "trace_id": trace_id,
                "replayed": False,
            },
        )

        if idem_key:
            await idempotency.finish(
                redis, str(user.id), idem_key,
                status="completed", content=full_text,
                conversation_id=str(conversation_id), usage=usage_dict,
            )

    except OpenAIError as exc:
        final_status = "failed"
        logger.warning("chat 模型调用失败: %s", exc)
        yield sse_event(EVENT_ERROR, {"code": "LLM_OFFLINE", "message": str(exc)})
        await _persist_failed(user, conversation_id, user_seq + 1, str(exc), trace_id)
        if idem_key:
            await idempotency.finish(
                redis, str(user.id), idem_key, status="failed",
                conversation_id=str(conversation_id),
                error={"code": "LLM_OFFLINE", "message": str(exc)},
            )
    except asyncio.CancelledError:
        # 客户端断开 / 超时取消：标记 interrupted 后原样抛出
        final_status = "interrupted"
        await _persist_failed(user, conversation_id, user_seq + 1, "客户端中断", trace_id,
                              status="interrupted")
        if idem_key:
            await idempotency.finish(
                redis, str(user.id), idem_key, status="failed",
                content="".join(full_text_parts), conversation_id=str(conversation_id),
                error={"code": "CONFLICT", "message": "生成被中断"},
            )
        raise
    except Exception as exc:  # noqa: BLE001 —— 兜底，保证流一定被正常收尾
        final_status = "failed"
        logger.exception("chat 内部错误")
        yield sse_event(EVENT_ERROR, {"code": "INTERNAL", "message": str(exc)})
        await _persist_failed(user, conversation_id, user_seq + 1, str(exc), trace_id)
        if idem_key:
            await idempotency.finish(
                redis, str(user.id), idem_key, status="failed",
                conversation_id=str(conversation_id),
                error={"code": "INTERNAL", "message": str(exc)},
            )
    finally:
        # 锁在任何路径下都释放（Lua 比对 token，防误删他人的锁）
        await release_conversation_lock(redis, str(conversation_id), lock_token)
        logger.info(
            "chat done conv=%s status=%s elapsed=%.2fs",
            conversation_id, final_status, time.perf_counter() - started,
        )


async def _persist_failed(
    user: UserCtx,
    conversation_id: UUID,
    seq: int,
    message: str,
    trace_id: str,
    status: str = "failed",
) -> None:
    """失败/中断的助手消息也要落库（status 标记，供断线自愈与排查）。"""
    try:
        async with tenant_session(user.id) as session:
            await conv_repo.insert_message(
                session,
                conversation_id=conversation_id,
                user_id=user.id,
                seq=seq,
                role="assistant",
                content=None,
                status=status,
                trace_id=trace_id,
                error={"message": message[:500]},
            )
            await conv_repo.touch_last_message(session, conversation_id)
    except Exception:  # noqa: BLE001 —— 失败落库本身不再抛错
        logger.exception("failed-message persist error conv=%s", conversation_id)


async def _replay_stream(payload: dict | None, trace_id: str) -> AsyncIterator[str]:
    """幂等重放：把上次完成的内容作为流再次下发（不调模型、不落库）。"""
    content = (payload or {}).get("content", "")
    yield sse_event(EVENT_TOKEN, {"delta": content})
    yield sse_event(
        EVENT_DONE,
        {
            "finish_reason": "stop",
            "conversation_id": (payload or {}).get("conversation_id", ""),
            "usage": (payload or {}).get("usage"),
            "trace_id": trace_id,
            "replayed": True,
        },
    )


async def _replay_error_stream(err: dict, trace_id: str) -> AsyncIterator[str]:
    yield sse_event(
        EVENT_ERROR,
        {"code": err.get("code", "INTERNAL"), "message": err.get("message", "上次请求失败")},
    )
    yield sse_event(EVENT_DONE, {"finish_reason": "error", "replayed": True, "trace_id": trace_id})
