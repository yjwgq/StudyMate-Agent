"""Planner 节点（M4-2，§7.1）：产出 DAG 计划。

输入用户消息 → LLM 输出严格 JSON：
    {"mode": "chat" | "plan",
     "steps": [{"id": 1, "description": "...", "tool_hint": "retrieval|search|todo|sandbox",
                "depends_on": []}, ...]}

健壮性要求（比聪明更重要）：
    - JSON 解析失败 / 步数 > 6 / 依赖不合法（自依赖、前向依赖、成环）
      → **回退 chat 模式** + degraded 标记。宁可退化成普通问答，也不挂。
    - 步数上限 6（§7.8 第 4 条「计划爆炸」防护）。
"""

import json
import logging
import re
from typing import Any

from agent.graph.schemas import TOOL_HINTS, Step
from agent.llm import AgentLLM, LLMResult

logger = logging.getLogger(__name__)

MAX_PLAN_STEPS = 6

PLANNER_SYSTEM_PROMPT = """你是任务规划器。判断用户消息需要哪种处理方式，输出严格 JSON（不要任何其他文字）：

1. 普通问答/闲聊/基于知识库的问题（一句话能答）→
   {"mode": "chat", "steps": []}

2. 需要工具或多步的任务（创建待办、计算、需要网络搜索、多方面调研）→
   {"mode": "plan", "steps": [
      {"id": 1, "description": "步骤描述", "tool_hint": "工具名或null", "depends_on": []}
   ]}

规则：
- steps 最多 6 个（{max_steps}）；把子任务合并，不要拆太碎；
- depends_on 里只能引用**更小**的 id（DAG 无环）；
- 独立的子步骤不要互相依赖（它们可以并行执行）；
- tool_hint 只能是：retrieval（知识库检索）/ search（联网搜索）/ todo（创建待办）/ sandbox（运行 Python 计算）/ null；
- 例子：用户问"调研 A 和 B 的区别" → 两个独立检索步（id 1、2，都无依赖）+ 一个汇总步（id 3，depends_on [1,2]，tool_hint null）。
"""


class PlanResult:
    def __init__(
        self,
        mode: str,
        steps: list[Step],
        *,
        raw: str = "",
        degraded_reason: str | None = None,
        tokens_used: int = 0,
    ) -> None:
        self.mode = mode
        self.steps = steps
        self.raw = raw
        self.degraded_reason = degraded_reason
        self.tokens_used = tokens_used


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠 JSON（容忍 ```json 包裹与前后废话）。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def validate_plan(data: dict[str, Any]) -> tuple[str, list[Step]] | None:
    """校验并规范化计划；不合法返回 None（调用方回退 chat）。"""
    mode = data.get("mode")
    if mode not in ("chat", "plan"):
        return None
    raw_steps = data.get("steps") or []
    if mode == "chat":
        return "chat", []
    if not isinstance(raw_steps, list) or not raw_steps:
        # plan 模式但没给步骤 → 视为 chat
        return "chat", []
    if len(raw_steps) > MAX_PLAN_STEPS:
        return None

    steps: list[Step] = []
    seen_ids: set[int] = set()
    for raw in raw_steps:
        if not isinstance(raw, dict):
            return None
        raw_id = raw.get("id")
        if not isinstance(raw_id, (int, str)):
            return None
        try:
            sid = int(raw_id)
        except (TypeError, ValueError):
            return None
        if sid in seen_ids or sid < 1 or sid > MAX_PLAN_STEPS:
            return None
        seen_ids.add(sid)
        desc = str(raw.get("description", "")).strip()
        if not desc:
            return None
        hint = raw.get("tool_hint")
        if hint is not None:
            hint = str(hint)
            if hint not in TOOL_HINTS:
                hint = None
        deps_raw = raw.get("depends_on") or []
        if not isinstance(deps_raw, list):
            return None
        try:
            deps = sorted({int(d) for d in deps_raw})
        except (TypeError, ValueError):
            return None
        # 依赖必须是已出现的更小 id（DAG 无环 + 无前向依赖）
        if any(d >= sid or d < 1 for d in deps):
            return None
        steps.append(
            Step(id=sid, description=desc, tool_hint=hint, depends_on=deps)
        )
    steps.sort(key=lambda s: s.id)
    return "plan", steps


async def run_planner(llm: AgentLLM, user_message: str) -> PlanResult:
    """调用模型产出计划。任何失败都回退 chat（不挂）+ 降级原因。"""
    # 提示词里含 JSON 花括号示例，不能用 str.format（会被当格式字段解析）
    system = PLANNER_SYSTEM_PROMPT.replace("{max_steps}", str(MAX_PLAN_STEPS))
    try:
        result: LLMResult = await llm.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
            span_name="planner",
        )
    except Exception as exc:  # noqa: BLE001 —— planner 失败不阻断
        logger.warning("planner 调用失败，回退 chat: %s", exc)
        return PlanResult("chat", [], degraded_reason="planner_error", raw=str(exc)[:200])

    data = extract_json(result.text)
    if data is None:
        logger.info("planner 输出非 JSON，回退 chat")
        return PlanResult(
            "chat", [], raw=result.text[:500], degraded_reason="planner_parse",
            tokens_used=result.usage.get("total_tokens") or 0,
        )
    parsed = validate_plan(data)
    if parsed is None:
        logger.info("planner 计划不合法，回退 chat")
        return PlanResult(
            "chat", [], raw=result.text[:500], degraded_reason="planner_invalid",
            tokens_used=result.usage.get("total_tokens") or 0,
        )
    mode, steps = parsed
    return PlanResult(
        mode, steps, raw=result.text[:500],
        tokens_used=result.usage.get("total_tokens") or 0,
    )
