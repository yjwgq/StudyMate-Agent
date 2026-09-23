"""POST /api/v1/chat —— M4 版本：Agent 运行时（Plan-and-Execute + DAG + ReAct）。

M1 保留的能力：JWT 认证、会话锁、幂等重放、失败落库、SSE 逐字回流。
M3 的单路 RAG 成为图的 chat 分支（planner 判定普通问答时走这条路径，
行为与 M3 验收口径一致）。本文件只保留「端点职责」：
    认证 / 会话 / 锁 / 幂等 / 消息落库 / SSE 桥接；
Agent 的全部逻辑在 agent/graph/（planner → chat|execute → synthesize）。

事件桥接（agent/graph/runner → SSE）：
    custom 事件 type ∈ {citations, token, notice, degraded, error} → 同名 SSE 事件
    final 摘要 → done 事件（citation_count / groundedness / degraded / usage / mode）
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from openai import OpenAIError
from pydantic import BaseModel, Field

from agent.graph.builder import get_checkpointer, run_agent_turn
from agent.llm import build_agent_llm
from agent.tools.base import ToolCtx
from agent.tools.builtin.retrieval_tool import build_builtin_tools
from agent.tools.policy import bootstrap_policies
from agent.tools.registry import ToolRegistry
from apps.api.core import idempotency
from apps.api.core.config import settings
from apps.api.core.db import tenant_session
from apps.api.core.deps import UserCtx, current_user
from apps.api.core.errors import AppError, ErrorCode, new_trace_id
from apps.api.core.locks import acquire_conversation_lock, release_conversation_lock
from apps.api.core.redis import get_redis
from apps.api.repositories import conversations as conv_repo
from apps.api.sse import (
    EVENT_CITATIONS,
    EVENT_DEGRADED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_NOTICE,
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
        description="会话 ID。缺省时自动创建新会话（done 事件返回新 id）。",
    )


async def get_llm_client() -> Any:
    """LLM 客户端工厂（M1 契约保留：测试用 dependency_overrides 注入 fake）。"""
    if not settings.llm_configured:
        raise AppError(
            ErrorCode.LLM_OFFLINE,
            "LLM 未配置：请在 .env 中填写 LLM_API_KEY 与 LLM_MODEL",
            503,
        )
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout_s,
    )


# ---------------- 工具注册表（进程级单例，MCP 惰性发现） ----------------

_registry: ToolRegistry | None = None
_registry_lock = asyncio.Lock()
_mcp_pool: Any = None


async def get_tool_registry() -> ToolRegistry:
    global _registry, _mcp_pool
    if _registry is not None:
        return _registry
    async with _registry_lock:
        if _registry is not None:
            return _registry
        bootstrap_policies()
        registry = ToolRegistry(redis=get_redis())
        for tool in build_builtin_tools():
            registry.register(tool)
        # 逐工具容错：单个工具策略缺失只影响该工具（fail-closed：不上架），
        # 不拖垮整个注册表
        # MCP 工具（sandbox/todo/search）：发现失败不阻断聊天（显式降级）。
        try:
            from agent.tools.mcp_client import McpClientPool, McpServerSpec

            _mcp_pool = McpClientPool(
                [
                    McpServerSpec("sandbox", "mcp_servers.sandbox_server", timeout_s=10.0),
                    McpServerSpec("todo", "mcp_servers.todo_server", timeout_s=10.0),
                    McpServerSpec("search", "mcp_servers.search_server", timeout_s=12.0),
                ]
            )
            for tool in await _mcp_pool.discover():
                try:
                    registry.register(tool)
                except ValueError as exc:
                    logger.warning("MCP 工具 %s 未上架：%s", tool.meta.name, exc)
        except Exception:  # noqa: BLE001
            logger.warning("MCP 工具发现失败，本轮仅内置工具可用", exc_info=True)
        _registry = registry
        return registry


@router.post("/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    user: Annotated[UserCtx, Depends(current_user)],
    llm: Annotated[Any, Depends(get_llm_client)],
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
            conv = await conv_repo.create_conversation(session, user.id, title=req.content[:60])
        conversation_id = str(conv["id"])
    cid = UUID(conversation_id)

    # ---- 3) 会话级锁（§7.4，B6）：串行独占；thread_id=conversation_id 的
    #         checkpoint 状态因此天然免并发写（§7.4 与 §7.3 的配套关系）----
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
    llm: Any,
    redis,
    user: UserCtx,
    conversation_id: UUID,
    content: str,
    user_seq: int,
    idem_key: str | None,
    lock_token: str,
    trace_id: str,
) -> AsyncIterator[str]:
    """Agent 图执行 + SSE 桥接 + 落库 + 幂等终态。"""
    from agent.obs import langfuse as obs

    started = time.perf_counter()
    trace = obs.start_chat_trace(
        trace_id,
        user_id=str(user.id),
        query=content,
        metadata={"conversation_id": str(conversation_id)},
    )
    agent_llm = build_agent_llm(llm, settings.llm_model, trace=trace)
    registry = await get_tool_registry()
    tool_ctx = ToolCtx(
        user_id=str(user.id),
        conversation_id=str(conversation_id),
        trace_id=trace_id,
        dedupe_seed=str(conversation_id),
        extra={"tool_events": []},
    )
    checkpointer = await get_checkpointer()

    answer = ""
    citations: list[dict] = []
    degraded_flags: list[str] = []
    usage_dict: dict[str, Any] | None = None
    grounded_stats: dict | None = None
    plan_summary: list[dict] = []
    mode = "chat"
    final_status = "completed"

    try:
        async for kind, item in run_agent_turn(
            llm=agent_llm,
            registry=registry,
            tool_ctx=tool_ctx,
            user_id=str(user.id),
            conversation_id=str(conversation_id),
            trace_id=trace_id,
            content=content,
            token_budget=30000,
            groundedness_enabled=settings.groundedness_enabled,
            checkpointer=checkpointer,
        ):
            if kind == "event":
                etype = item.get("type")
                if etype == "token":
                    answer += str(item.get("delta") or "")
                    yield sse_event(EVENT_TOKEN, {"delta": item["delta"]})
                elif etype == "citations":
                    citations = item.get("citations") or []
                    yield sse_event(EVENT_CITATIONS, {"citations": citations})
                elif etype == "notice":
                    yield sse_event(EVENT_NOTICE, item)
                elif etype == "degraded":
                    degraded_flags = item.get("degraded") or degraded_flags
                    yield sse_event(EVENT_DEGRADED, item)
                elif etype == "error":
                    yield sse_event(EVENT_ERROR, {
                        "code": item.get("code", "INTERNAL"),
                        "message": item.get("message", "执行失败"),
                    })
            else:  # final
                answer = str(item.get("answer") or answer)
                citations = item.get("citations") or citations
                degraded_flags = item.get("degraded") or degraded_flags
                grounded_stats = item.get("groundedness")
                mode = str(item.get("mode") or mode)
                # E1 验收证据：各 step 起止时间戳（并行执行 → 区间重叠）
                plan_summary = [
                    {
                        "id": p.get("id"),
                        "desc": (p.get("description") or "")[:40],
                        "status": p.get("status"),
                        "started_at": p.get("started_at"),
                        "finished_at": p.get("finished_at"),
                        "attempts": p.get("attempts"),
                    }
                    for p in (item.get("plan") or [])
                ]
                tokens = item.get("tokens_used") or 0
                if tokens:
                    usage_dict = {"total_tokens": tokens}
                if item.get("error"):
                    logger.warning("graph 返回错误: %s", item["error"])

        elapsed = time.perf_counter() - started
        async with tenant_session(user.id) as session:
            assistant = await conv_repo.insert_message(
                session,
                conversation_id=conversation_id,
                user_id=user.id,
                seq=user_seq + 1,
                role="assistant",
                content=answer,
                status="completed",
                token_usage=usage_dict,
                trace_id=trace_id,
                citations=citations or None,
                degraded=(
                    {"flags": degraded_flags, "groundedness": grounded_stats}
                    if degraded_flags or grounded_stats else None
                ),
            )
            await conv_repo.touch_last_message(session, conversation_id)

        done_payload: dict[str, Any] = {
            "finish_reason": "stop",
            "conversation_id": str(conversation_id),
            "message_id": str(assistant["id"]),
            "elapsed_s": round(elapsed, 2),
            "usage": usage_dict,
            "trace_id": trace_id,
            "replayed": False,
            "citation_count": len(citations),
            "groundedness": grounded_stats,
            "degraded": degraded_flags,
            "mode": mode,
            "tools_used": len([e for e in tool_ctx.extra.get("tool_events", []) if e.get("ok")]),
            "plan": plan_summary,
        }
        yield sse_event(EVENT_DONE, done_payload)

        if idem_key:
            await idempotency.finish(
                redis, str(user.id), idem_key,
                status="completed", content=answer,
                conversation_id=str(conversation_id), usage=usage_dict,
                citations=citations or None,
            )
        trace.end(output={
            "status": "completed", "mode": mode,
            "citations": len(citations), "groundedness": grounded_stats,
            "degraded": degraded_flags, "usage": usage_dict,
        })

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
        trace.end(output={"status": "failed", "error": str(exc)})
    except asyncio.CancelledError:
        final_status = "interrupted"
        await _persist_failed(user, conversation_id, user_seq + 1, "客户端中断", trace_id,
                              status="interrupted")
        if idem_key:
            await idempotency.finish(
                redis, str(user.id), idem_key, status="failed",
                content=answer, conversation_id=str(conversation_id),
                error={"code": "CONFLICT", "message": "生成被中断"},
            )
        trace.end(output={"status": "interrupted"})
        raise
    except Exception as exc:  # noqa: BLE001 —— 兜底，保证流正常收尾
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
        trace.end(output={"status": "failed", "error": str(exc)})
    finally:
        # 锁在任何路径下都释放（Lua 比对 token，防误删他人的锁）
        await release_conversation_lock(redis, str(conversation_id), lock_token)
        await obs.flush_async()
        logger.info(
            "chat done conv=%s status=%s mode=%s elapsed=%.2fs",
            conversation_id, final_status, mode, time.perf_counter() - started,
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
    """幂等重放：引用面板先于正文（M3 契约），不调模型、不落库。"""
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
