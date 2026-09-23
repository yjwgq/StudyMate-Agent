"""Agent 轮次的后台执行与事件通道（M5-3/4/5，§7.7 执行与传输解耦）。

核心机制：
    - POST /chat 只投递任务：建 assistant 消息（streaming）→ asyncio.create_task
      执行本轮 → 立即返回 {message_id, stream_url}。执行不绑定 HTTP 生命周期
      （F4：页面关了任务照跑，答案完整落库）。
    - 事件通道 = Redis Stream `chat:ev:{message_id}`（TTL 1h）：
      后台任务 XADD，订阅端 XRANGE 补历史 + XREAD 阻塞实时（M5-3 最简版：
      token 增量不承诺重放，断线后以落库内容为准）。
    - 取消：`chat:cancel:{message_id}` 标记，执行循环在**每个事件**处检查
      （粒度够细：生成 1 token 都能停），命中即抛 TurnCancelled。
    - 审批（M5-2）：图因 interrupt 暂停 → 消息置 interrupted + done 事件
      {finish_reason: awaiting_approval}；批准/拒绝后 resume_turn 以
      Command(resume=...) 从 checkpoint 续跑，事件继续写同一条消息的 stream。
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from agent.obs import langfuse as obs
from apps.api.core.config import settings
from apps.api.core.db import tenant_session
from apps.api.core.redis import get_redis
from apps.api.repositories import conversations as conv_repo

logger = logging.getLogger(__name__)

STREAM_TTL_S = 3600
CANCEL_TTL_S = 3600
DONE_FINISH = "stop"


class TurnCancelled(Exception):
    """用户取消（POST /chat/{mid}/cancel）。"""


def _ev_key(message_id: str) -> str:
    return f"chat:ev:{message_id}"


def _cancel_key(message_id: str) -> str:
    return f"chat:cancel:{message_id}"


def _sid(entry_id) -> str:
    """Redis entry id → str（redis-py 在未开 decode_responses 时返回 bytes）。"""
    return entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)


def _field(fields, name: str):
    """Stream fields 取值（key 可能是 bytes 或 str，取决于连接配置）。"""
    if name in fields:
        return fields[name]
    for k, v in fields.items():
        if (k if isinstance(k, str) else k.decode()) == name:
            return v
    return None


@dataclass
class TurnSpec:
    """一次后台执行的全部上下文。"""

    llm: Any                      # AsyncOpenAI 兼容客户端（依赖注入的 fake/真实）
    user: Any                     # UserCtx
    conversation_id: UUID
    message_id: UUID              # assistant 消息（streaming）
    user_seq: int                 # 用户消息 seq（assistant = user_seq + 1）
    content: str
    trace_id: str
    idem_key: str | None = None
    resume_command: Any = None    # 非空 = 审批恢复（Command(resume=...)）
    extra: dict = field(default_factory=dict)


async def publish_event(redis, message_id: str, event: dict[str, Any]) -> str:
    """事件写入 Redis Stream，返回 entry id（订阅端的 last_event_id 锚点）。"""
    entry_id = await redis.xadd(
        _ev_key(message_id), {"e": json.dumps(event, ensure_ascii=False)}
    )
    await redis.expire(_ev_key(message_id), STREAM_TTL_S)
    return entry_id


async def is_cancelled(redis, message_id: str) -> bool:
    return bool(await redis.exists(_cancel_key(message_id)))


async def run_turn_task(spec: TurnSpec) -> None:
    """后台任务主体：执行图 → 事件入流 → 消息终态 → 幂等 → 释放锁。

    初始化（trace/registry/checkpointer）也在 try 内 —— M5 验收实锤：
    初始化抛错时任务静默死亡（无事件、无终态、锁泄漏），订阅端只能看到
    STREAM_GONE。任何早期失败都必须落 error 事件 + failed 终态。
    """
    redis = get_redis()
    mid = str(spec.message_id)
    started = asyncio.get_running_loop().time()
    answer = ""
    citations: list[dict] = []
    degraded_flags: list[str] = []
    grounded_stats: dict | None = None
    mode = "chat"
    usage_dict: dict[str, Any] | None = None
    awaiting: dict | None = None
    final_status = "completed"
    tools_used = 0

    async def emit(event: dict[str, Any]) -> None:
        # 取消检查点：每个事件（含每个 token）都查 —— 粒度到 token 级
        if await is_cancelled(redis, mid):
            raise TurnCancelled()
        await publish_event(redis, mid, event)

    try:
        from agent.graph.builder import get_checkpointer, run_agent_turn
        from agent.llm import build_agent_llm
        from agent.tools.base import ToolCtx
        from apps.api.api.v1.chat_deps import get_tool_registry

        trace = obs.start_chat_trace(
            spec.trace_id,
            user_id=str(spec.user.id),
            query=spec.content or "(resume)",
            metadata={"conversation_id": str(spec.conversation_id), "message_id": mid},
        )
        agent_llm = build_agent_llm(spec.llm, settings.llm_model, trace=trace)
        registry = await get_tool_registry()
        registry._redis = get_redis()  # 工具层幂等去重（进程级单例注入）
        tool_ctx = ToolCtx(
            user_id=str(spec.user.id),
            conversation_id=str(spec.conversation_id),
            message_id=mid,
            trace_id=spec.trace_id,
            dedupe_seed=str(spec.conversation_id),
            extra={"tool_events": [], "redis": redis},  # redis：工具去重 + 决策记忆化
        )
        checkpointer = await get_checkpointer()
        if spec.resume_command is not None:
            await _update_message(spec, status="streaming")
        async for kind, item in run_agent_turn(
            llm=agent_llm,
            registry=registry,
            tool_ctx=tool_ctx,
            user_id=str(spec.user.id),
            conversation_id=str(spec.conversation_id),
            message_id=mid,
            trace_id=spec.trace_id,
            content=spec.content,
            token_budget=30000,
            groundedness_enabled=settings.groundedness_enabled,
            checkpointer=checkpointer,
            resume_command=spec.resume_command,
        ):
            if kind == "event":
                etype = item.get("type")
                if etype == "token":
                    answer += str(item.get("delta") or "")
                await emit(item)
            else:  # final
                answer = str(item.get("answer") or answer)
                citations = item.get("citations") or citations
                degraded_flags = item.get("degraded") or degraded_flags
                grounded_stats = item.get("groundedness")
                mode = str(item.get("mode") or mode)
                tokens = item.get("tokens_used") or 0
                if tokens:
                    usage_dict = {"total_tokens": tokens}
                awaiting = item.get("awaiting_approval")
                if item.get("error"):
                    logger.warning("graph 返回错误: %s", item["error"])
                tools_used = len(
                    [e for e in tool_ctx.extra.get("tool_events", []) if e.get("ok")]
                )

        if awaiting:
            # 图因 L2 审批暂停（§7.3）：消息 interrupted（保留已生成内容），
            # 锁释放，等人工决策后 resume_turn 续跑（F3 的关键路径）
            final_status = "interrupted"
            await _update_message(
                spec, status="interrupted", content=answer, citations=citations,
                degraded=_degraded_payload(degraded_flags, grounded_stats),
                token_usage=usage_dict,
            )
            # 审批事件必须先于 done 下发（前端审批卡片的数据源）。
            # 说明：registry 的 ctx.emit 未接线（工具层拿不到图内 stream writer），
            # 故由 run_turn_task 在图暂停后统一补发 —— 语义等价且只有一处出口。
            await emit({
                "type": "approval",
                "approval_id": awaiting.get("approval_id"),
                "tool": awaiting.get("tool"),
                "args": awaiting.get("args"),
                "risk_level": awaiting.get("risk_level"),
                "reason": awaiting.get("reason"),
            })
            await emit({
                "type": "done",
                "finish_reason": "awaiting_approval",
                "message_id": mid,
                "approval": awaiting,
                "content_snapshot": answer,
            })
            trace.end(output={"status": "awaiting_approval", "approval": awaiting})
        else:
            await _finish_completed(spec, answer, citations, degraded_flags,
                                    grounded_stats, usage_dict, mode, tools_used, emit)
            trace.end(output={"status": "completed", "mode": mode,
                              "citations": len(citations), "usage": usage_dict})

    except TurnCancelled:
        final_status = "cancelled"
        await _update_message(spec, status="cancelled", content=answer, citations=citations,
                              degraded=_degraded_payload(degraded_flags, grounded_stats))
        await publish_event(redis, mid, {
            "type": "done", "finish_reason": "cancelled",
            "message_id": mid, "content_snapshot": answer,
        })
        if spec.idem_key:
            await _idem_finish(spec, status="failed", content=answer,
                               error={"code": "CANCELLED", "message": "用户取消"})
        trace.end(output={"status": "cancelled"})
    except Exception as exc:  # noqa: BLE001 —— 后台任务兜底：消息必须落终态
        final_status = "failed"
        logger.exception("turn 执行失败 mid=%s", mid)
        await _update_message(spec, status="failed", error={"message": str(exc)[:500]})
        await publish_event(redis, mid, {
            "type": "error", "code": "INTERNAL", "message": str(exc),
        })
        await publish_event(redis, mid, {"type": "done", "finish_reason": "error"})
        if spec.idem_key:
            await _idem_finish(spec, status="failed", error={"code": "INTERNAL", "message": str(exc)[:300]})
        trace.end(output={"status": "failed", "error": str(exc)})
    finally:
        from apps.api.core.locks import release_conversation_lock

        await release_conversation_lock(redis, str(spec.conversation_id), spec.extra["lock_token"])
        await obs.flush_async()
        logger.info(
            "turn done mid=%s status=%s mode=%s elapsed=%.2fs",
            mid, final_status, mode, asyncio.get_running_loop().time() - started,
        )


async def _finish_completed(
    spec: TurnSpec, answer: str, citations: list[dict], degraded_flags: list[str],
    grounded_stats: dict | None, usage_dict: dict | None, mode: str, tools_used: int,
    emit: Any,
) -> None:
    await _update_message(spec, status="completed", content=answer, citations=citations,
                          degraded=_degraded_payload(degraded_flags, grounded_stats),
                          token_usage=usage_dict)
    await emit({
        "type": "done",
        "finish_reason": "stop",
        "replayed": False,
        "message_id": str(spec.message_id),
        "usage": usage_dict,
        "citation_count": len(citations),
        "groundedness": grounded_stats,
        "degraded": degraded_flags,
        "mode": mode,
        "tools_used": tools_used,
    })
    if spec.idem_key:
        await _idem_finish(spec, status="completed", content=answer,
                           citations=citations or None, usage=usage_dict)


async def _update_message(
    spec: TurnSpec, *, status: str, content: str | None = None,
    citations: list[dict] | None = None, degraded: dict | None = None,
    token_usage: dict | None = None, error: dict | None = None,
) -> None:
    async with tenant_session(spec.user.id) as session:
        await conv_repo.update_message(
            session,
            message_id=spec.message_id,
            user_id=spec.user.id,
            status=status,
            content=content,
            citations=citations,
            degraded=degraded,
            token_usage=token_usage,
            error=error,
        )
        await conv_repo.touch_last_message(session, spec.conversation_id)


def _degraded_payload(flags: list[str], grounded_stats: dict | None) -> dict | None:
    return {"flags": flags, "groundedness": grounded_stats} if (flags or grounded_stats) else None


async def _idem_finish(
    spec: TurnSpec, *, status: str, content: str = "",
    citations: list[dict] | None = None, usage: dict | None = None,
    error: dict | None = None,
) -> None:
    from apps.api.core import idempotency

    idem_key = spec.idem_key or ""
    await idempotency.finish(
        get_redis(), str(spec.user.id), idem_key,
        status=status, content=content, conversation_id=str(spec.conversation_id),
        usage=usage, citations=citations, error=error,
    )


# ---------------- 订阅端（GET /chat/{mid}/stream 的实现体） ----------------


async def iter_stream_events(redis, message_id: str, last_event_id: str | None) -> Any:
    """SSE 事件迭代器：补历史（XRANGE）→ 阻塞实时（XREAD）直到 done。

    断线重连语义（§7.7 最简版）：last_event_id 之后的事件可续；
    token 增量不承诺补齐 —— 前端重连时以消息落库内容为准。

    启动窗口（M5 验收实锤）：后台任务在首个事件前要做 MCP 工具发现
    （spawn 多个子进程，可达数秒）——订阅端此时查不到 stream key 属正常，
    必须给宽限期，否则误判 STREAM_GONE 把正常轮次杀死。
    """
    import time

    key = _ev_key(message_id)
    min_id = "0"
    if last_event_id:
        # Redis 6.2+ 独占区间语法：(id
        min_id = f"({last_event_id}"
    # XRANGE 支持 (id 独占语法；XREAD 的 last_id 语义本身就是"大于"，不支持 (
    xrange_cursor = min_id
    xread_cursor = last_event_id or "0-0"
    grace_until = time.monotonic() + 60.0  # key 首次出现的等待上限
    ever_seen = bool(await redis.exists(key))
    while True:
        rows = await redis.xrange(key, min=xrange_cursor, max="+", count=100)
        if rows:
            ever_seen = True
            for entry_id, fields in rows:
                payload = json.loads(_field(fields, "e") or "{}")
                yield _sid(entry_id), payload
                if payload.get("type") == "done":
                    return
            xrange_cursor = "(" + _sid(rows[-1][0])
            xread_cursor = _sid(rows[-1][0])
            continue
        rows = await redis.xread({key: xread_cursor}, block=5000, count=100)
        if rows:
            for _key, entries in rows:
                for _entry_id, fields in entries:
                    payload = json.loads(_field(fields, "e") or "{}")
                    yield _sid(_entry_id), payload
                    if payload.get("type") == "done":
                        return
            xread_cursor = _sid(entries[-1][0])
            xrange_cursor = "(" + xread_cursor
            ever_seen = True
        else:
            exists_now = bool(await redis.exists(key))
            if exists_now:
                ever_seen = True
                continue  # key 在但 5s 无新事件：任务可能还在跑（工具执行中）
            if ever_seen:
                # 出现过又消失：TTL 过期/重启丢失 → 显式终止
                yield "0-0", {"type": "error", "code": "STREAM_GONE",
                              "message": "事件流已过期，请刷新页面获取最终内容"}
                yield "0-1", {"type": "done", "finish_reason": "error"}
                return
            if time.monotonic() > grace_until:
                yield "0-0", {"type": "error", "code": "STREAM_TIMEOUT",
                              "message": "等待生成启动超时，请稍后刷新页面"}
                yield "0-1", {"type": "done", "finish_reason": "error"}
                return
            # 启动窗口内：继续等
            await asyncio.sleep(0.5)
