"""Groundedness 逐句校验（M3-3 / D1，§8.4）。

回答的每个「事实性论断」句都应带来源编号 [n]。本模块统计无出处事实句占比：

    ratio = 无出处事实句数 / 事实性论断句数

    ratio > GROUNDEDNESS_THRESHOLD (0.20) → 触发一次针对性重写（§8.4）；
    重写仍不达标 → messages.degraded 落标记，UI 标黄「该结论缺少资料支撑」。

「事实性论断」的判定是启发式（MVP 不引入 NLI 模型）：
    - 句长 ≥ 8 个有效字符、含 CJK —— 过滤客套/空句；
    - 短句白名单：纯过渡/致谢语（「希望这能帮到你」）不计入；
    - 列表/表格行剥掉 markdown 修饰后按正文处理（列表同样可能承载事实）。

这是 D1「用 groundedness 校验输出统计，不靠肉眼判断」的量化依据。
NLI 风格的模型级核验留给 M7 评测体系（RAGAS faithfulness），此处刻意保持轻量。
"""

import re
from dataclasses import dataclass

from apps.api.core.config import settings

# 句子切分：句末标点（中英）收尾或到换行为止，让 markdown 列表行各自成句
_SENT = re.compile(r"[^。！？；!?;\n]+[。！？；!?;]?")
# 引用编号 [1] / [1][3] / [12]
_CITATION = re.compile(r"\[\d{1,2}\]")
# markdown 行首修饰：列表、标题、引用块、表格
_MD_PREFIX = re.compile(r"^\s*(?:[-*+]\s+|\d+[.、)]\s*|#{1,6}\s*|>\s*|\|)")
_CJK = re.compile(r"[\u4e00-\u9fff]")
# 短过渡/致谢白名单：命中且句子较短时不算事实句
_TRANSITIONS = (
    "希望", "你好", "您好", "欢迎", "请问", "以上就是", "以下是",
    "综上", "总而言之", "如有其他", "随时", "仅供参考",
)
_TRANSITION_MAX_LEN = 30
# 「资料未提及 X」是**元陈述**（关于资料覆盖范围的忠实声明），不是关于世界的
# 事实论断 —— 恰恰是提示词要求模型做的正确行为（§8.4「资料不足时明确说明」），
# 计为"无出处"会造成系统性误判（D1 实测：13 句判定里 2 句属于此类，全被误伤）。
# 局限：模型理论上可用这句话逃避引用义务，但那属于答案层评测
# （RAGAS faithfulness，M7）的范畴，不在这里用启发式硬兜。
_META_ABSENCE = re.compile(
    r"(资料|材料|文档|上下文|原文|文中)(中|里)?(均|都|也)?(未|没有|不)"
    r"(提及|列出|明确|说明|涉及|涵盖|包含|提供|给出|交代)"
)


@dataclass
class GroundednessStats:
    total_claims: int = 0        # 事实性论断句数
    cited_claims: int = 0        # 带来源编号的事实句数
    unsupported_ratio: float = 0.0
    ok: bool = True              # ratio <= threshold（§8.4：>20% 才触发重写）
    threshold: float = 0.20

    def as_dict(self) -> dict:
        return {
            "total_claims": self.total_claims,
            "cited_claims": self.cited_claims,
            "unsupported_ratio": round(self.unsupported_ratio, 4),
            "threshold": self.threshold,
            "ok": self.ok,
        }


def _is_table_structure(stripped: str) -> bool:
    """表格结构行（表头 / 分隔行）：单元格都是短标签，不承载事实陈述。

    D1 实测：模型用 markdown 表格对比概念时，表头行
    `| 对比维度 | 监督学习 | 无监督学习 |` 会被误判为无出处事实句。
    数据行不受影响 —— 它们承载内容（单元格更长）且通常带引用。
    """
    if "|" not in stripped:
        return False
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    if not cells:
        return False
    return all(len(c) <= 8 or set(c) <= set("-: ") for c in cells)


def _is_claim(sentence: str) -> bool:
    """是否为「事实性论断」句（启发式，见模块 docstring）。"""
    if _CJK.search(sentence) is None:
        return False
    stripped = _MD_PREFIX.sub("", sentence).strip()
    # 白名单短句（客套/过渡）与过短句（≥6 有效字符为限）不算事实论断
    if len(stripped) < 6:
        return False
    # 元陈述（「资料未提及 X」）不是事实论断 —— 这是模型被要求做的忠实声明
    if _META_ABSENCE.search(stripped):
        return False
    # 表格结构行（表头/分隔行）同样不承载事实
    if _is_table_structure(stripped):
        return False
    return not (
        len(stripped) <= _TRANSITION_MAX_LEN and any(t in stripped for t in _TRANSITIONS)
    )


def split_sentences(text: str) -> list[str]:
    """把回答切成句（保留标点），供逐句校验与调试台展示。"""
    return [m.group().strip() for m in _SENT.finditer(text) if m.group().strip()]


def check_groundedness(text: str, *, threshold: float | None = None) -> GroundednessStats:
    """统计无出处事实句占比。total=0（如纯客套）视为通过。"""
    th = settings.groundedness_threshold if threshold is None else threshold
    stats = GroundednessStats(threshold=th)
    for sentence in split_sentences(text):
        if not _is_claim(sentence):
            continue
        stats.total_claims += 1
        if _CITATION.search(sentence):
            stats.cited_claims += 1
    if stats.total_claims > 0:
        stats.unsupported_ratio = 1 - stats.cited_claims / stats.total_claims
    stats.ok = stats.unsupported_ratio <= th
    return stats
