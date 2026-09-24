"""Chat 端点 —— M5 版本：执行与传输解耦（§7.7 最简版）。

POST /api/v1/chat                       投递任务 → {message_id, stream_url}
GET  /api/v1/chat/{message_id}/stream   订阅事件流（SSE，支持 Last-Event-ID 续读）
POST /api/v1/chat/{message_id}/cancel   取消生成（消息标 cancelled，保留已生成内容）
POST /api/v1/chat/{message_id}/regenerate  重新生成（旧消息 superseded_by=新消息）

语义变化（M4 → M5）：
    - POST 不再是长连接 SSE：执行移入后台任务（asyncio.create_task），
      事件写 Redis Stream chat:ev:{message_id}，执行不绑定 HTTP 生命周期
      —— 页面关闭任务照跑（F4），进程死亡后审批可恢复（F3）；
    - 幂等重放保持 M3 语义：同 Idempotency-Key 的已完成请求在 POST 直接
      返回重放 SSE（先 citations 后正文）；
    - 会话锁（B6）语义不变：锁在 POST 获取、后台任务结束时释放。

M1/M3 保留：JWT 认证、RLS 租户隔离、groundedness、Langfuse 全链路观测。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from apps.api.api.v1 import chat_runtime
from apps.api.api.v1.chat_deps import get_llm_client
from apps.api.api.v1.chat_runtime import TurnSpec, run_turn_task
from apps.api.core import idempotency
from apps.api.core.db import tenant_session
from apps.api.core.deps import UserCtx, current_user
from apps.api.core.errors import AppError, ErrorCode, new_trace_id
from apps.api.core.locks import acquire_conversation_lock, release_conversation_lock
from apps.api.core.redis import get_redis
from apps.api.repositories import conversations as conv_repo
from apps.api.sse import (
    EVENT_CITATIONS,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_TOKEN,
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    sse_event,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000, description="用户输入")
    conversation_id: UUID | None = Field(
        default=None,
        description="会话 ID。缺省时自动创建新会话。",
    )


@router.post("/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    user: Annotated[UserCtx, Depends(current_user)],
    llm: Annotated[Any, Depends(get_llm_client)],
    idem_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
) -> Any:
    """投递一轮对话任务（执行与传输解耦，§7.7）。"""
    trace_id = getattr(request.state, "trace_id", "") or new_trace_id()
    redis = get_redis()

    # ---- 1) 幂等：已完成 / 已失败 → 直接重放 SSE（M3 语义，不调模型）----
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

    # ---- 2) 会话归属 / 创建 ----
    if req.conversation_id is not None:
        async with tenant_session(user.id) as session:
            conv = await conv_repo.get_conversation(session, user.id, req.conversation_id)
        if conv is None:
            raise AppError(ErrorCode.NOT_FOUND, "会话不存在", 404)
        conversation_id = str(req.conversation_id)
    else:
        async with tenant_session(user.id) as session:
            conv = await conv_repo.create_conversation(session, user.id, title=req.content[:60])
        conversation_id = str(conv["id"])
    cid = UUID(conversation_id)

    # ---- 3) 会话锁（B6；thread_id=conversation_id 的 checkpoint 免并发写）----
    lock_token = await acquire_conversation_lock(redis, conversation_id)
    if lock_token is None:
        raise AppError(
            ErrorCode.CONFLICT, "该会话正在处理中，请稍后再试", 409,
            headers={"Retry-After": "5"},
        )

    # ---- 4) 幂等占位（锁内）----
    if idem_key:
        status, payload = await idempotency.acquire(redis, str(user.id), idem_key)
        if status != "acquired":
            await release_conversation_lock(redis, conversation_id, lock_token)
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

    # ---- 5) 用户消息 + assistant 占位消息（streaming）落库（M5-4）----
    async with tenant_session(user.id) as session:
        seq = await conv_repo.next_seq(session, cid)
        await conv_repo.insert_message(
            session, conversation_id=cid, user_id=user.id, seq=seq,
            role="user", content=req.content, status="completed", trace_id=trace_id,
        )
        assistant = await conv_repo.insert_message(
            session, conversation_id=cid, user_id=user.id, seq=seq + 1,
            role="assistant", content=None, status="streaming", trace_id=trace_id,
        )

    # ---- 6) 投递后台任务（执行与 HTTP 解耦，§7.7 第 1 条）----
    spec = TurnSpec(
        llm=llm, user=user, conversation_id=cid, message_id=UUID(str(assistant["id"])),
        user_seq=seq, content=req.content, trace_id=trace_id, idem_key=idem_key,
        extra={"lock_token": lock_token},
    )
    asyncio.create_task(run_turn_task(spec))

    return {
        "data": {
            "message_id": str(assistant["id"]),
            "conversation_id": conversation_id,
            "stream_url": f"/api/v1/chat/{assistant['id']}/stream",
        },
        "trace_id": trace_id,
    }


@router.get("/chat/{message_id}/stream")
async def stream_chat(
    message_id: UUID,
    user: Annotated[UserCtx, Depends(current_user)],
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """订阅一轮生成的事件流（SSE）。

    断线重连：带 Last-Event-ID 续读该位置之后的事件（§7.7 第 3/4 条）；
    token 增量不承诺重放 —— 前端重连后以消息落库内容为准。
    """
    redis = get_redis()
    async with tenant_session(user.id) as session:
        message = await conv_repo.get_message(session, user.id, message_id)
    if message is None:
        raise AppError(ErrorCode.NOT_FOUND, "消息不存在", 404)

    async def _gen() -> AsyncIterator[str]:
        async for entry_id, event in chat_runtime.iter_stream_events(
            redis, str(message_id), last_event_id
        ):
            etype = event.get("type")
            if etype == "token":
                yield sse_event(EVENT_TOKEN, event, event_id=entry_id)
            elif etype == "citations":
                yield sse_event(EVENT_CITATIONS, event, event_id=entry_id)
            elif etype == "error":
                yield sse_event(EVENT_ERROR, event, event_id=entry_id)
            else:  # done / notice / degraded / approval / tool_*：原样透传
                yield sse_event(etype or "message", event, event_id=entry_id)

    return StreamingResponse(
        _gen(),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


class RegenerateRequest(BaseModel):
    content: str | None = Field(
        default=None, max_length=8000,
        description="覆盖内容；缺省用原 user 消息",
    )


@router.post("/chat/{message_id}/cancel")
async def cancel_chat(
    message_id: UUID,
    user: Annotated[UserCtx, Depends(current_user)],
) -> dict:
    """取消生成（§7.7 第 6 条）：标记 + 后台任务在下一个事件处退出。

    消息终态 cancelled、保留已生成内容；不计入评测/记忆。
    """
    redis = get_redis()
    async with tenant_session(user.id) as session:
        message = await conv_repo.get_message(session, user.id, message_id)
    if message is None:
        raise AppError(ErrorCode.NOT_FOUND, "消息不存在", 404)
    if message["status"] not in ("streaming", "interrupted"):
        raise AppError(ErrorCode.CONFLICT, f"消息状态为 {message['status']}，无法取消", 409)
    await redis.set(chat_runtime._cancel_key(str(message_id)), "1",
                    ex=chat_runtime.CANCEL_TTL_S)
    return {"data": {"ok": True, "message_id": str(message_id)}}


@router.post("/chat/{message_id}/regenerate")
async def regenerate_chat(
    message_id: UUID,
    request: RegenerateRequest,
    user: Annotated[UserCtx, Depends(current_user)],
    request_http: Request,
) -> dict:
    """重新生成（§7.7 第 7 条）：旧消息 superseded_by=新消息，新任务投递。"""
    trace_id = getattr(request_http.state, "trace_id", "") or new_trace_id()
    redis = get_redis()
    llm = await get_llm_client()

    async with tenant_session(user.id) as session:
        old = await conv_repo.get_message(session, user.id, message_id)
        if old is None:
            raise AppError(ErrorCode.NOT_FOUND, "消息不存在", 404)
        if old["role"] != "assistant":
            raise AppError(ErrorCode.VALIDATION, "只能重新生成助手消息", 422)
        user_msg = await conv_repo.get_user_message_before(
            session, user.id, UUID(str(old["conversation_id"])), old["seq"]
        )
        if user_msg is None:
            raise AppError(ErrorCode.CONFLICT, "找不到对应的用户消息", 409)

        content = (request.content or user_msg["content"] or "").strip()
        cid = UUID(str(old["conversation_id"]))
        # 会话锁（重新生成与新消息互斥）
        lock_token = await acquire_conversation_lock(redis, str(old["conversation_id"]))
        if lock_token is None:
            raise AppError(ErrorCode.CONFLICT, "该会话正在处理中，请稍后再试", 409,
                           headers={"Retry-After": "5"})
        seq = await conv_repo.next_seq(session, cid)
        new_message = await conv_repo.insert_message(
            session, conversation_id=cid, user_id=user.id, seq=seq,
            role="assistant", content=None, status="streaming", trace_id=trace_id,
        )
        # 旧消息保留并打标（不原地覆盖，§7.7 第 7 条）
        await conv_repo.update_message(
            session, message_id=message_id, user_id=user.id,
            status=old["status"], superseded_by=UUID(str(new_message["id"])),
        )

    spec = TurnSpec(
        llm=llm, user=user, conversation_id=cid, message_id=UUID(str(new_message["id"])),
        user_seq=seq, content=content, trace_id=trace_id,
        extra={"lock_token": lock_token},
    )
    asyncio.create_task(run_turn_task(spec))
    return {
        "data": {
            "message_id": str(new_message["id"]),
            "conversation_id": str(cid),
            "superseded": str(message_id),
            "stream_url": f"/api/v1/chat/{new_message['id']}/stream",
        },
        "trace_id": trace_id,
    }


# ---------------- 幂等重放（M3 语义） ----------------


async def _replay_stream(payload: dict | None, trace_id: str) -> AsyncIterator[str]:
    data = payload or {}
    citations = data.get("citations") or []
    if citations:
        yield sse_event(EVENT_CITATIONS, {"citations": citations})
    yield sse_event(EVENT_TOKEN, {"delta": data.get("content", "")})
    yield sse_event(
        EVENT_DONE,
        {
            "finish_reason": "stop",
            "conversation_id": data.get("conversation_id", ""),
            "usage": data.get("usage"),
            "trace_id": trace_id,
            "replayed": True,
            "citation_count": len(citations),
        },
    )


async def _replay_error_stream(err: dict, trace_id: str) -> AsyncIterator[str]:
    yield sse_event(
        EVENT_ERROR,
        {"code": err.get("code", "INTERNAL"), "message": err.get("message", "上次请求失败")},
    )
    yield sse_event(EVENT_DONE, {"finish_reason": "error", "replayed": True, "trace_id": trace_id})
