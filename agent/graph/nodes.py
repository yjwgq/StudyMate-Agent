"""图节点实现（M4）：planner / chat 分支 / execute 分支 / synthesize。

chat 分支是 M3 单路 RAG 管线的移植（检索 → citations 事件 → 流式生成 →
groundedness 校验可重写一次）—— 行为与 M3 验收保持一致，事件经 LangGraph
stream writer 发出，由 runner 桥接成 SSE。

事件协议（custom stream → SSE）：
    {"type": "citations", "citations": [...]}
    {"type": "token", "delta": "..."}
    {"type": "notice", "code": "...", "message": "...", "clear": true}
    {"type": "degraded", "degraded": [...], "message": "..."}
"""

import logging
import time
from typing import Any

from langgraph.config import get_stream_writer

from agent.graph.budget import BudgetState
from agent.graph.executor import execute_dag
from agent.graph.planner import run_planner
from agent.graph.schemas import Citation, Step
from agent.graph.state import AgentState
from agent.guardrails.groundedness import check_groundedness
from agent.retrieval.context import (
    NO_CONTEXT_SYSTEM_PROMPT,
    RAG_SYSTEM_PROMPT,
    REWRITE_SYSTEM_PROMPT,
    build_context_blocks,
)
from agent.retrieval.search import build_citations, get_retriever
from agent.tools.base import ToolCtx
from agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _writer() -> Any:
    """安全的 stream writer：图外调用（单测直跑节点）时静默 no-op。"""
    try:
        return get_stream_writer()
    except Exception:  # noqa: BLE001
        return lambda _event: None


def _emit(event: dict[str, Any]) -> None:
    _writer()(event)


def _degraded_event(flags: list[str], message: str) -> dict[str, Any]:
    return {"type": "degraded", "degraded": flags, "message": message}


# ---------------- planner 节点 ----------------


async def planner_node(state: AgentState, *, llm: Any) -> dict[str, Any]:
    """产出 DAG 计划；失败回退 chat + degraded。"""
    user_msg = _last_user_text(state["messages"])
    result = await run_planner(llm, user_msg)
    update: dict[str, Any] = {
        "mode": result.mode,
        "plan": result.steps,
        "replans_left": 1,
        "tokens_used": result.tokens_used,  # sum_int 累加
    }
    if result.degraded_reason:
        update["degraded"] = [result.degraded_reason]
    logger.info("planner mode=%s steps=%s degraded=%s", result.mode, len(result.steps), result.degraded_reason)
    return update


def route_after_planner(state: AgentState) -> str:
    return "chat_node" if state.get("mode", "chat") == "chat" else "execute_node"


def _last_user_text(messages: list) -> str:
    """取最后一条用户消息。

    兼容两种形态：输入期是 dict（role="user"），经 add_messages 合并/
    checkpoint 恢复后是 LangChain 消息对象（type="human"）—— 只认 "user"
    会拿到空串（M4 验收实锤：planner 对空问题规划，全链路假答）。
    """
    for m in reversed(messages or []):
        if isinstance(m, dict):
            if m.get("role") == "user":
                content = m.get("content", "")
                return content if isinstance(content, str) else str(content)
        else:
            mtype = getattr(m, "type", "")
            if mtype in ("human", "user"):
                content = getattr(m, "content", "")
                return content if isinstance(content, str) else str(content)
    return ""


# ---------------- chat 分支（M3 RAG 管线移植） ----------------


async def _retrieve(user_id: str, query: str) -> tuple[list, list[dict], str, list[str]]:
    degraded: list[str] = []
    retriever = get_retriever()
    try:
        qvec = await retriever.embed_query(query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("query embedding 失败，降级无 RAG: %s", exc)
        return [], [], "", ["retrieval"]
    from uuid import UUID

    from apps.api.core.db import tenant_session

    try:
        async with tenant_session(UUID(user_id)) as session:
            hits = await retriever.search(session, UUID(user_id), query, qvec=qvec)
    except Exception as exc:  # noqa: BLE001
        logger.warning("检索查询失败，降级无 RAG: %s", exc)
        return [], [], "", ["retrieval"]
    citations = build_citations(hits)
    return hits, citations, build_context_blocks(hits) if hits else "", degraded


def _build_rag_messages(content: str, context_blocks: str) -> list[dict[str, str]]:
    if context_blocks:
        system = RAG_SYSTEM_PROMPT + "\n\n可用资料：\n" + context_blocks
    else:
        system = NO_CONTEXT_SYSTEM_PROMPT
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


async def chat_node(state: AgentState, *, llm: Any, groundedness_enabled: bool = True) -> dict[str, Any]:
    """M3 单路 RAG 的图内移植：行为与 M3 验收口径一致。"""
    content = _last_user_text(state["messages"])
    degraded_flags: list[str] = list(state.get("degraded") or [])
    citations: list[dict] = []
    grounded_stats: dict | None = None

    hits, citations, context_blocks, degraded = await _retrieve(state["user_id"], content)
    degraded_flags.extend(degraded)
    if citations:
        _emit({"type": "citations", "citations": citations})

    answer, usage_total = await _generate_with_groundedness(
        llm=llm,
        content=content,
        context_blocks=context_blocks,
        has_citations=bool(citations),
        groundedness_enabled=groundedness_enabled,
    )
    if answer is None:
        # 生成失败：交给 runner 走错误路径
        return {"error": "LLM_OFFLINE", "degraded": degraded_flags, "citations": citations}

    # groundedness 统计（无引用时不校验，与 M3 一致）
    stats = check_groundedness(answer) if (citations and groundedness_enabled) else None
    if stats is not None:
        grounded_stats = stats.as_dict()
        if not stats.ok:
            degraded_flags.append("groundedness")
            _emit(_degraded_event(["groundedness"], "部分结论缺少资料支撑，已标注，请谨慎采信。"))

    tokens = usage_total.get("total_tokens") or 0
    return {
        "answer": answer,
        "citations": citations,
        "retrieved": _hits_to_chunks(hits),
        "groundedness": grounded_stats,
        "degraded": degraded_flags,
        "tokens_used": tokens,
        "rounds_used": 1,
    }


async def _generate_with_groundedness(
    *,
    llm: Any,
    content: str,
    context_blocks: str,
    has_citations: bool,
    groundedness_enabled: bool,
) -> tuple[str | None, dict[str, int | None]]:
    """流式生成 + 引用不足重写一次。返回 (最终文本, usage)。失败返回 (None, {})。"""
    usage_total: dict[str, int | None] = {}

    async def _run_pass(messages: list[dict[str, str]]) -> str:
        parts: list[str] = []
        async for delta, usage in llm.stream(messages, span_name="llm"):
            if usage:
                for k, v in usage.items():
                    if v is not None:
                        usage_total[k] = (usage_total.get(k) or 0) + v
            if delta:
                parts.append(delta)
                _emit({"type": "token", "delta": delta})
        return "".join(parts)

    messages = _build_rag_messages(content, context_blocks)
    pass1 = await _run_pass(messages)

    if has_citations and groundedness_enabled:
        stats = check_groundedness(pass1)
        if not stats.ok:
            _emit(
                {
                    "type": "notice",
                    "code": "GROUNDEDNESS_REWRITE",
                    "message": "部分论断缺少引用来源，正在重新生成…",
                    "clear": True,
                }
            )
            rewrite_messages = [
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT + "\n\n可用资料：\n" + context_blocks},
                {"role": "user", "content": content},
            ]
            pass2 = await _run_pass(rewrite_messages)
            if pass2.strip():
                pass1 = pass2

    return pass1, usage_total


def _hits_to_chunks(hits: list) -> list:
    from agent.graph.schemas import Chunk

    return [
        Chunk(
            chunk_id=str(h.child_chunk_id),
            parent_chunk_id=str(h.parent_chunk_id) if h.parent_chunk_id else None,
            document_id=str(h.document_id),
            document_title=h.document_title,
            score=h.score,
            content=h.child_content,
            parent_content=h.parent_content,
            page=h.page,
        )
        for h in hits
    ]


# ---------------- execute 分支 ----------------


async def execute_node(
    state: AgentState, *, llm: Any, registry: ToolRegistry, ctx: ToolCtx, token_budget: int
) -> dict[str, Any]:
    plan: list[Step] = list(state.get("plan") or [])
    budget = BudgetState(token_budget=token_budget)
    budget.spend(state.get("tokens_used") or 0)
    result = await execute_dag(
        llm=llm, registry=registry, ctx=ctx, plan=plan, budget=budget,
        max_parallel=state.get("max_parallel") or 3,
        replans_left=state.get("replans_left") or 1,
    )
    update: dict[str, Any] = {
        "plan": result["plan"],
        "tokens_used": result["tokens_used"],
        "rounds_used": sum(o.rounds_used for o in result["outcomes"].values()),
    }
    all_chunks = []
    all_citations = []
    for o in result["outcomes"].values():
        all_chunks.extend(o.chunks)
        all_citations.extend(o.citations)
    if all_chunks:
        update["retrieved"] = all_chunks
    if all_citations:
        update["citations"] = all_citations
    if result["replanned"]:
        update["degraded"] = ["replan"]
    return update


# ---------------- synthesize ----------------

SYNTHESIZE_SYSTEM_PROMPT = """你是汇总器。基于以下各步骤的结论与资料，为用户生成最终回答。

要求：
1. 来自资料的事实性论断，句末标注来源编号 [n]（n 对应下方资料编号）；
2. 步骤失败或资料不足的部分，明确说明，不要编造；
3. 直接回答用户，不要复述步骤编号。"""


async def synthesize_node(state: AgentState, *, llm: Any, user_query: str) -> dict[str, Any]:
    started = time.perf_counter()
    plan: list[Step] = list(state.get("plan") or [])
    citations: list[Citation] = list(state.get("citations") or [])

    # 引用全局重编号（并行 step 的局部编号在此统一）
    renumbered: list[Citation] = []
    id_map: dict[str, int] = {}
    for c in citations:
        key = str(c.chunk_id)
        if key not in id_map:
            id_map[key] = len(renumbered) + 1
            renumbered.append(c.model_copy(update={"n": len(renumbered) + 1}))
    citations = renumbered

    sections: list[str] = []
    for s in sorted(plan, key=lambda x: x.id):
        if s.status == "done" and s.result:
            sections.append(f"### 步骤 {s.id}：{s.description}\n{s.result}")
        elif s.status == "failed":
            sections.append(f"### 步骤 {s.id}：{s.description}\n[该步骤失败：{(s.error or '')[:120]}]")
        elif s.status == "skipped":
            sections.append(f"### 步骤 {s.id}：{s.description}\n[该步骤未执行：{(s.error or '')[:80]}]")

    blocks: list[str] = []
    for c in citations:
        page = f"（第 {c.page} 段）" if c.page is not None else ""
        blocks.append(f"[{c.n}] 《{c.title}》{page}\n{c.snippet}")

    partial_note = ""
    if any(s.status in ("failed", "skipped") for s in plan):
        partial_note = "（注意：部分步骤未能完成，请如实说明覆盖范围的局限。）"

    messages = [
        {"role": "system", "content": SYNTHESIZE_SYSTEM_PROMPT + (f"\n\n{partial_note}" if partial_note else "")},
        {"role": "user", "content": user_query},
    ]
    if sections:
        messages.append({"role": "user", "content": "各步骤结论：\n\n" + "\n\n".join(sections)})
    if blocks:
        messages.append({"role": "user", "content": "可用资料（编号供引用）：\n\n" + "\n\n".join(blocks)})

    parts: list[str] = []
    usage_total: dict[str, int | None] = {}
    async for delta, usage in llm.stream(messages, span_name="llm"):
        if usage:
            for k, v in usage.items():
                if v is not None:
                    usage_total[k] = (usage_total.get(k) or 0) + v
        if delta:
            parts.append(delta)
            _emit({"type": "token", "delta": delta})
    answer = "".join(parts)

    degraded_flags = list(state.get("degraded") or [])
    grounded_stats = None
    if citations:
        stats = check_groundedness(answer)
        grounded_stats = stats.as_dict()
        if not stats.ok:
            degraded_flags.append("groundedness")
            _emit(_degraded_event(["groundedness"], "部分结论缺少资料支撑，已标注，请谨慎采信。"))

    logger.info("synthesize 完成 elapsed=%.2fs citations=%d", time.perf_counter() - started, len(citations))
    return {
        "answer": answer,
        "citations": citations,
        "groundedness": grounded_stats,
        "degraded": degraded_flags,
        "tokens_used": usage_total.get("total_tokens") or 0,
    }
