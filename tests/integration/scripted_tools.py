"""脚本化 LLM 与测试工具桩（M4-12）。

ScriptedLLM：
    planner 调用 → 按 plan_key 返回预设 JSON 计划；
    react 调用   → 按「当前步骤：{desc}」里的关键词路由到该 step 的脚本队列，
                   逐个弹出；队列耗尽返回兜底 final 文本（防死循环）；
    stream 调用  → synthesize/chat 的最终生成（返回合成文本）。
"""

import asyncio
import json
from typing import Any

from agent.llm import LLMResult, ToolCall


def tool_call(id_: str, name: str, args: dict[str, Any]) -> ToolCall:
    return ToolCall(id=id_, name=name, arguments=args)


class ScriptedLLM:
    def __init__(self, *, plan: str, scripts: dict[str, list], final: str = "合成答案") -> None:
        self.plan = plan
        self.scripts = scripts  # {step 描述关键词: [ToolCall|str, ...]}
        self.final = final
        self.calls = 0
        self.complete_calls: list[dict] = []

    async def complete(self, messages, *, tools=None, temperature=0.3, span_name="llm") -> LLMResult:
        self.calls += 1
        self.complete_calls.append({"span": span_name, "messages": messages})
        if span_name == "planner":
            return LLMResult(text=self.plan, usage={"total_tokens": 12}, finish_reason="stop")
        # react：按 step 关键词路由；索引进度 = 历史里的 tool 结果条数。
        # 不能 pop（有状态）：LangGraph 在 interrupt 恢复时会**从头重跑节点** ——
        # 已推进的队列会让重跑直接返回结论、跳过审批后的工具执行
        # （M5 验收实锤：approvals 一直停在 pending）。真实 LLM 依据历史响应，
        # 因此按「该步已产生的 tool 结果条数」索引，重跑天然幂等。
        user_texts = [m.get("content", "") for m in messages if m.get("role") == "user"]
        joined = " ".join(user_texts)
        tool_count = sum(1 for m in messages if m.get("role") == "tool")
        for key, queue in self.scripts.items():
            if key in joined:
                usage = {"total_tokens": 21}
                index = min(tool_count, len(queue))
                if index >= len(queue):
                    return LLMResult(
                        text=f"[{key}] 脚本耗尽，给出结论。", usage=usage, finish_reason="stop"
                    )
                item = queue[index]
                if isinstance(item, str):
                    return LLMResult(text=item, usage=usage, finish_reason="stop")
                return LLMResult(tool_calls=[item], usage=usage, finish_reason="tool_calls")
        raise AssertionError(f"ScriptedLLM: 未匹配任何 step 脚本：{joined[:80]}")

    async def stream(self, messages, *, temperature=0.3, span_name="llm"):
        # synthesize / chat：逐字吐 final（模拟流式）
        for ch in self.final:
            yield ch, {}
        yield None, {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45}


# ---------------- 工具桩（E1 并行 / E4 截断） ----------------

from pydantic import Field  # noqa: E402

from agent.tools.base import BaseTool, ToolArgs, ToolCtx, ToolMeta, ToolResult  # noqa: E402


class EmptyArgs(ToolArgs):
    pass


class SlowTool(BaseTool):
    """E1：耗时 0.8s 的只读工具 —— 两个 step 并行调用时墙钟 < 串行两倍。"""

    meta = ToolMeta(
        name="slow_probe",
        description="测试桩：慢速只读工具（0.8s）",
        args_schema=EmptyArgs,
        risk_level=0,
        idempotent=True,
        timeout_s=5.0,
    )

    async def arun(self, ctx: ToolCtx, **kwargs: Any) -> ToolResult:
        await asyncio.sleep(0.8)
        return ToolResult(ok=True, content="slow-probe-done")


class HugeTool(BaseTool):
    """E4：返回超长内容 → 注册表截断 + full_ref。"""

    meta = ToolMeta(
        name="huge_probe",
        description="测试桩：返回 20000 字符内容",
        args_schema=EmptyArgs,
        risk_level=0,
        idempotent=True,
        timeout_s=5.0,
        max_result_chars=4000,
    )

    async def arun(self, ctx: ToolCtx, **kwargs: Any) -> ToolResult:
        return ToolResult(ok=True, content="X" * 20000)


class ProbeArgs(ToolArgs):
    q: str = Field(default="")


def plan_json(steps: list[dict[str, Any]]) -> str:
    return json.dumps({"mode": "plan", "steps": steps}, ensure_ascii=False)
