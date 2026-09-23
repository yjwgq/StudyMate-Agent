"""上下文与 token 预算管理（M4-5，§7.8）。

三层控制的实现划分：
    1. 工具结果截断 → 在 ToolRegistry（max_result_chars + full_ref）；
    2. 滚动压缩   → 本模块 `compress_history`（ReAct 轮次 > 4 触发）；
    3. 预算阈值   → 本模块 `BudgetState`（80% 停止新工具调用，100% 部分答案）。

token 估算：没有 tokenizer 依赖（tiktok 体积大且云端模型分词不公开），
用「CJK 字符 ≈ 1 token、其他 ≈ 1/4 token」的保守估计 —— 只用于预算
阈值判断，误差 ±30% 可接受（阈值本身是软限制，超了也只是提前收尾）。
"""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

CJK_RANGE = (0x4E00, 0x9FFF)


def estimate_tokens(text: str) -> int:
    """保守估算：CJK 每字 1 token，其他按 4 字符 1 token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if CJK_RANGE[0] <= ord(ch) <= CJK_RANGE[1])
    return cjk + (len(text) - cjk) // 4 + 1


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        total += estimate_tokens(str(m.get("content", "")))
        for tc in m.get("tool_calls", []) or []:
            total += estimate_tokens(str(tc.get("function", {}).get("arguments", "")))
    return total


# ---------------- 预算阈值（§7.8 第 3 条） ----------------


@dataclass
class BudgetState:
    """单轮对话的预算状态。executor / react 共享读取，executor 单点累加。"""

    token_budget: int = 30000
    used: int = 0

    def spend(self, tokens: int) -> None:
        self.used += max(0, tokens)

    @property
    def ratio(self) -> float:
        return self.used / self.token_budget if self.token_budget > 0 else 0.0

    def may_start_new_tools(self) -> bool:
        """≥80%：停止开启新的工具调用，直接进入 synthesize（§7.8）。"""
        return self.ratio < 0.8

    def at_hard_limit(self) -> bool:
        """≥100%：用已得结果生成「部分答案」并显式标注。"""
        return self.ratio >= 1.0

    def status_note(self) -> str:
        if self.at_hard_limit():
            return "因预算限制未能完成全部步骤，以下为部分答案。"
        if not self.may_start_new_tools():
            return "已接近预算上限，停止继续调用工具，直接汇总现有结果。"
        return ""


# ---------------- 滚动压缩（§7.8 第 2 条） ----------------

COMPRESS_AFTER_ROUNDS = 4
"""ReAct 轮次超过该值后，把更早的轮次压缩为结构化摘要。"""


@dataclass
class CompressedHistory:
    """压缩产物：保留近 N 轮原文 + 早期轮次的结构化摘要。"""

    summary: str = ""
    kept_messages: list[dict[str, Any]] = field(default_factory=list)
    compressed_rounds: int = 0


def compress_history(
    messages: list[dict[str, Any]],
    *,
    llm: Any = None,
    keep_last_rounds: int = COMPRESS_AFTER_ROUNDS,
) -> CompressedHistory:
    """把早期 ReAct 轮次压缩为一条结构化摘要。

    压缩本身可以是一次 LLM 调用（计入预算，§7.8）；M4 先用**确定性抽取**
    （工具名 + 关键参数 + 结果首行）—— 不花 token、无噪声，多数场景够用；
    llm 参数预留给 M6/M8 升级为 LLM 摘要。

    返回值由调用方用于重建消息列表：[system] + [summary 作为 user 约束]
    + 近 N 轮原文。原始内容移出上下文（但 tool_invocations 表保留全量）。
    """
    if len(messages) <= keep_last_rounds * 2:
        return CompressedHistory(kept_messages=messages)

    # 一轮 ≈ assistant(tool_calls) + tool 结果。以 assistant 含 tool_calls 切分
    round_starts: list[int] = []
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            round_starts.append(i)
    if len(round_starts) <= keep_last_rounds:
        return CompressedHistory(kept_messages=messages)

    split_at = round_starts[-keep_last_rounds]
    early, kept = messages[:split_at], messages[split_at:]

    summary_lines = ["[此前工具调用摘要]"]
    for m in early:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments", "")
                if isinstance(args, str):
                    args = args[:120]
                summary_lines.append(f"- {fn.get('name')}({args})")
        elif m.get("role") == "tool":
            content = str(m.get("content", ""))
            first_line = content.splitlines()[0][:160] if content else ""
            summary_lines.append(f"  → {first_line}")
    return CompressedHistory(
        summary="\n".join(summary_lines),
        kept_messages=kept,
        compressed_rounds=len(round_starts) - keep_last_rounds,
    )
