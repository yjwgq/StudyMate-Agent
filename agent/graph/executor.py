"""DAG 执行器（M4-3，§7.1 execute_dag）。

调度循环：
    1. 取依赖满足的 pending steps → 并发上限 max_parallel（默认 3）内
       asyncio.gather 并行执行（每个 step 内部是 ReAct 循环，§7.3）；
    2. step 失败 → 重试当前 step（上限 2 次重试）；仍失败标记 failed，
       依赖它的后续 steps 标记 skipped —— 不阻塞其他分支；
    3. 重规划（replans_left 上限 1）：存在失败分支且还有 pending steps
       时，把失败上下文交回 planner 产出新计划替换剩余部分（M4 简版）；
    4. 预算 80% → 不再启动新 step；100% → 剩余 steps 全部 skipped，
       由 synthesize 产出「部分答案」（§7.8）。

E1 的核心证据在这里：独立 steps 并行执行 —— Step.started_at / finished_at
记录时间戳，波次内时间戳重叠即并行成立。
"""

import asyncio
import logging
import time
from typing import Any

from agent.graph.budget import BudgetState
from agent.graph.planner import PlanResult, run_planner
from agent.graph.react import StepOutcome, run_react_step
from agent.graph.schemas import Step
from agent.llm import AgentLLM
from agent.tools.base import ToolCtx
from agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_RETRIES_PER_STEP = 2


def _ready_steps(plan: list[Step]) -> list[Step]:
    """依赖全部满足（done）的 pending steps，按 id 稳定排序。"""
    status_by_id = {s.id: s.status for s in plan}
    ready = [
        s
        for s in plan
        if s.status == "pending"
        and all(status_by_id.get(d) == "done" for d in s.depends_on)
    ]
    return sorted(ready, key=lambda s: s.id)


def _dependents_of(plan: list[Step], step_id: int) -> list[int]:
    return [s.id for s in plan if step_id in s.depends_on]


def _skip_dependents(plan: list[Step], failed_ids: set[int]) -> list[Step]:
    """失败 step 的传递依赖标记 skipped（新列表，禁原地改 —— §7.2 规范 1）。"""
    changed = True
    skipped: set[int] = set(failed_ids)
    while changed:
        changed = False
        for s in plan:
            if s.status == "pending" and any(d in skipped for d in s.depends_on):
                skipped.add(s.id)
                changed = True
    return [
        s.model_copy(update={"status": "skipped", "error": "依赖的步骤失败"})
        if s.id in skipped and s.status == "pending"
        else s
        for s in plan
    ]


async def _run_one_step(
    *,
    llm: AgentLLM,
    registry: ToolRegistry,
    ctx: ToolCtx,
    step: Step,
    budget: BudgetState,
    tools_schema: list[dict[str, Any]],
    prior_results: str,
) -> tuple[Step, StepOutcome]:
    """带重试的单 step 执行（§7.1 step_router：可重试 → 上限 2 次重试）。"""
    outcome = StepOutcome(step_id=step.id)
    attempts = 0
    started = time.perf_counter()
    plan_step = step.model_copy(update={"status": "running", "started_at": started})
    while attempts <= MAX_RETRIES_PER_STEP:
        plan_step = plan_step.model_copy(update={"attempts": attempts})
        outcome = await run_react_step(
            llm=llm,
            registry=registry,
            ctx=ctx,
            step_id=step.id,
            step_desc=step.description,
            budget=budget,
            tools_schema=tools_schema,
            prior_context=prior_results if attempts == 0 else "",
        )
        if outcome.ok or budget.at_hard_limit():
            break
        if outcome.loop_detected:
            # 循环不算执行失败（T4 已终止并要求模型给结论），重试无意义
            break
        attempts += 1
        if attempts <= MAX_RETRIES_PER_STEP:
            logger.info("step=%s 第 %s 次重试（error=%r）", step.id, attempts, outcome.error)
    finished = time.perf_counter()
    plan_step = plan_step.model_copy(
        update={
            "status": "done" if outcome.ok else "failed",
            "result": outcome.result or (outcome.error or ""),
            "error": outcome.error,
            "finished_at": finished,
            "tokens_used": outcome.tokens_used,
        }
    )
    return plan_step, outcome


async def execute_dag(
    *,
    llm: AgentLLM,
    registry: ToolRegistry,
    ctx: ToolCtx,
    plan: list[Step],
    budget: BudgetState,
    max_parallel: int = 3,
    replans_left: int = 1,
    prior_context: str = "",
) -> dict[str, Any]:
    """执行整张 DAG。返回：
        {
          "plan": 更新后的 steps（新列表）,
          "outcomes": {step_id: StepOutcome},
          "tokens_used": int,
          "replanned": bool,
          "all_done": bool,
        }
    """
    working = [s.model_copy() for s in plan]
    outcomes: dict[int, StepOutcome] = {}
    tools_schema = registry.openai_schemas()
    replanned = False

    while True:
        # 预算：80% 不再启动新 step（已运行的自然跑完），100% 直接收尾
        ready = _ready_steps(working)
        if not ready:
            break
        if budget.at_hard_limit():
            working = [
                s.model_copy(update={"status": "skipped", "error": "预算耗尽"})
                if s.status == "pending"
                else s
                for s in working
            ]
            break
        if not budget.may_start_new_tools():
            working = [
                s.model_copy(update={"status": "skipped", "error": "接近预算上限，未启动"})
                if s.status == "pending"
                else s
                for s in working
            ]
            break

        wave = ready[:max_parallel]
        logger.info(
            "DAG wave: steps=%s parallel=%d budget=%.0f%%",
            [s.id for s in wave], max_parallel, budget.ratio * 100,
        )
        results = await asyncio.gather(
            *(
                _run_one_step(
                    llm=llm, registry=registry, ctx=ctx, step=s,
                    budget=budget, tools_schema=tools_schema,
                    prior_results=prior_context,
                )
                for s in wave
            )
        )
        for plan_step, outcome in results:
            outcomes[outcome.step_id] = outcome
            budget.spend(outcome.tokens_used)
            working = [plan_step if s.id == plan_step.id else s for s in working]

        # 失败处理：跳过传递依赖；预算允许且还有希望 → 重规划一次
        failed = {s.id for s in working if s.status == "failed"}
        if failed:
            working = _skip_dependents(working, failed)
            pending_left = [s for s in working if s.status == "pending"]
            if pending_left and replans_left > 0 and not budget.at_hard_limit():
                replans_left -= 1
                replanned = True
                failure_note = "；".join(
                    f"步骤{s.id}({s.description[:40]})失败：{(s.error or '')[:80]}"
                    for s in working
                    if s.id in failed
                )
                replan_result: PlanResult = await run_planner(
                    llm,
                    f"原任务的以下步骤失败：{failure_note}。"
                    "请为**剩余工作**重新规划（不要重复已完成的部分）。",
                )
                if replan_result.mode == "plan" and replan_result.steps:
                    # 新计划接在已完成分支之后：id 续号，避免与旧 id 冲突
                    next_id = max((s.id for s in working), default=0) + 1
                    shift = next_id - 1
                    new_steps = [
                        s.model_copy(update={"id": s.id + shift, "depends_on": [d + shift for d in s.depends_on]})
                        for s in replan_result.steps
                    ]
                    working = working + new_steps
                logger.info("replan 完成，新增 %s 个步骤", len(replan_result.steps))

    tokens = sum(o.tokens_used for o in outcomes.values())
    all_done = all(s.status in ("done", "skipped") for s in working) and not any(
        s.status == "failed" for s in working
    )
    return {
        "plan": working,
        "outcomes": outcomes,
        "tokens_used": tokens,
        "replanned": replanned,
        "all_done": all_done,
    }
