"""ReAct 执行器（M4-4，§7.3）。

实现为 async 函数而非 LangGraph 子图：每轮循环在函数内推进，状态私有
（轮次计数 / 已见调用指纹），结束时一次性回传结果 —— 避免 checkpoint
放大与并行子图复杂度；四类终止条件与循环检测（验收核心）全部显式实现。

终止条件（§7.3 四选一，全部显式判断）：
    T1 无新工具调用（模型给出最终答案）
    T2 轮次超过 MAX_ROUNDS=8
    T3 token 预算达到阈值（80% 停止新调用；100% 产出部分答案，§7.8）
    T4 同一工具以相同参数重复调用（循环检测，防死循环）——E5

每轮：LLM 决策（function calling）→ 命中工具 → ToolRegistry.invoke
→ Observation 回灌。工具失败（ToolError）作为 Observation 回灌而不是
抛出 —— 让模型有机会换路径；但同一工具连续失败 2 次则终止本 step。
"""

import json
import logging
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from typing import Any

from agent.graph.budget import BudgetState, compress_history, estimate_messages_tokens
from agent.graph.schemas import Chunk, Citation
from agent.llm import AgentLLM
from agent.tools.base import ToolCtx, ToolError, ToolResult
from agent.tools.builtin.retrieval_tool import parse_hits_from_result
from agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_ROUNDS = 8
MAX_TOOL_CONSECUTIVE_FAILURES = 2

REACT_SYSTEM_PROMPT = """你是执行器，负责完成一个具体步骤。你可以调用工具收集信息，然后给出该步骤的结论。

规则：
- 需要信息就调用工具；信息足够后**直接输出结论文本**，不要再调用工具；
- 结论要具体、自包含（后续会与其他步骤的结果汇总给用户）；
- 工具失败时可以换参数重试或换工具；同一调用不要重复发起；
- 最多 {max_rounds} 轮。"""

LOOP_OBSERVATION = "[循环检测] 相同工具与相同参数刚被调用过，结果不会变化。请基于已有结果直接给出结论。"
BUDGET_OBSERVATION = "[预算] 已达到预算上限，无法继续调用工具。请基于已有信息给出当前结论。"


@dataclass
class StepOutcome:
    """一个 step 的执行结果（executor 聚合进 state）。"""

    step_id: int
    ok: bool = False
    result: str = ""
    error: str | None = None
    rounds_used: int = 0
    tokens_used: int = 0
    loop_detected: bool = False
    budget_stopped: bool = False
    citations: list[Citation] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)


def _build_tool_messages(
    *,
    step_desc: str,
    history: list[dict[str, Any]],
    tools_schema: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": REACT_SYSTEM_PROMPT.format(max_rounds=MAX_ROUNDS)},
        {"role": "user", "content": f"当前步骤：{step_desc}"},
        *history,
    ]


def _hits_to_state(hits: list[dict[str, Any]], *, start_n: int) -> tuple[list[Citation], list[Chunk]]:
    """检索命中 → 引用与 chunk（局部编号从 start_n 起，synthesize 前全局重排）。"""
    citations: list[Citation] = []
    chunks: list[Chunk] = []
    for i, h in enumerate(hits):
        chunks.append(
            Chunk(
                chunk_id=str(h.get("chunk_id")),
                parent_chunk_id=str(h.get("parent_chunk_id")) if h.get("parent_chunk_id") else None,
                document_id=str(h.get("document_id")),
                document_title=str(h.get("document_title", "")),
                score=float(h.get("score", 0.0)),
                content=str(h.get("content", "")),
                parent_content=str(h.get("parent_content", "")),
                page=h.get("page"),
            )
        )
        citations.append(
            Citation(
                n=start_n + i,
                chunk_id=str(h.get("chunk_id")),
                parent_chunk_id=str(h.get("parent_chunk_id")) if h.get("parent_chunk_id") else None,
                document_id=str(h.get("document_id")),
                title=str(h.get("document_title", "")),
                page=h.get("page"),
                snippet=str(h.get("content", ""))[:200],
                score=float(h.get("score", 0.0)),
            )
        )
    return citations, chunks


async def run_react_step(
    *,
    llm: AgentLLM,
    registry: ToolRegistry,
    ctx: ToolCtx,
    step_id: int,
    step_desc: str,
    budget: BudgetState,
    tools_schema: list[dict[str, Any]] | None = None,
    prior_context: str = "",
    max_rounds: int = MAX_ROUNDS,
) -> StepOutcome:
    """执行单个 step 的 ReAct 循环。见模块 docstring 的终止条件。"""
    outcome = StepOutcome(step_id=step_id)
    tools_schema = tools_schema if tools_schema is not None else registry.openai_schemas()
    history: list[dict[str, Any]] = []
    if prior_context:
        history.append({"role": "user", "content": f"此前步骤的结论（供参考）：\n{prior_context}"})

    seen_calls: set[str] = set()          # T4 循环检测指纹：tool + canonical args
    consecutive_failures = 0
    local_citation_n = 1

    for round_no in range(1, max_rounds + 1):
        outcome.rounds_used = round_no

        # T3：预算阈值（发起调用前检查 —— 80% 起停止新工具调用）
        if not budget.may_start_new_tools() and round_no > 1:
            outcome.budget_stopped = True
            outcome.result = outcome.result or budget.status_note()
            logger.info("react step=%s 预算 %s 停止", step_id, f"{budget.ratio:.0%}")
            break

        messages = _build_tool_messages(
            step_desc=step_desc, history=history, tools_schema=tools_schema
        )
        try:
            result = await llm.complete(messages, tools=tools_schema or None, span_name="react")
        except Exception as exc:  # noqa: BLE001 —— 模型失败终止本 step
            outcome.error = f"模型调用失败：{exc}"
            return outcome
        outcome.tokens_used += result.usage.get("total_tokens") or estimate_messages_tokens(messages)

        # T1：无工具调用 → 本步结论
        if not result.has_tool_calls:
            outcome.ok = True
            outcome.result = result.text.strip() or "（无结论）"
            return outcome

        # 压缩检查（§7.8 第 2 条：>4 轮触发）
        if round_no > 1 and len(history) > (round_no - 1) * 3:
            compressed = compress_history(history)
            if compressed.summary:
                history = [{"role": "user", "content": compressed.summary}, *compressed.kept_messages]

        # 执行工具调用（单个 assistant 消息可能带多个 tool_calls，串行执行）
        history.append(
            {
                "role": "assistant",
                "content": result.text or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                    }
                    for tc in result.tool_calls
                ],
            }
        )
        stop_after_tools = False
        for tc in result.tool_calls:
            fingerprint = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False)}"
            if fingerprint in seen_calls:
                # T4：循环检测 —— E5 的核心
                outcome.loop_detected = True
                history.append({"role": "tool", "tool_call_id": tc.id, "content": LOOP_OBSERVATION})
                continue
            seen_calls.add(fingerprint)

            try:
                tool_result: ToolResult = await registry.invoke(
                    tc.name, tc.arguments, dc_replace(ctx, step_id=step_id, round=round_no)
                )
                consecutive_failures = 0 if tool_result.ok else consecutive_failures + 1
                observation = tool_result.content
                hits = parse_hits_from_result(tool_result)
                if hits:
                    citations, chunks = _hits_to_state(hits, start_n=local_citation_n)
                    local_citation_n += len(citations)
                    outcome.citations.extend(citations)
                    outcome.chunks.extend(chunks)
            except ToolError as exc:
                consecutive_failures += 1
                observation = f"[工具失败 {exc.code}] {exc.message}"
                tool_result = ToolResult(ok=False, content=observation, error_code=exc.code)

            # 注意：不再在这里切片 —— ToolRegistry 是唯一截断点（§7.5），
            # 它在截断时附加的 full_ref 提示必须完整到达模型（M4 验收实锤：
            # 二次切片会恰好切掉 full_ref 后缀，E4 假失败）
            history.append({"role": "tool", "tool_call_id": tc.id, "content": observation})

        # 连续失败过多：不再给模型机会，直接终止
        if consecutive_failures >= MAX_TOOL_CONSECUTIVE_FAILURES:
            outcome.error = f"工具连续失败 {consecutive_failures} 次，终止本步骤"
            return outcome
        if stop_after_tools:
            break

    # T2：轮次耗尽
    outcome.error = outcome.error or f"已达最大轮次 {max_rounds}"
    return outcome
