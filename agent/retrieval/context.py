"""RAG 上下文拼装与提示词（M3-3，§8.4）。

引用契约（提示词与校验器必须成对出现）：
    - 上下文块以 [n] 编号，n 与 citations JSONB 的 n 一一对应；
    - 要求模型「每个事实句末尾标注来源编号」；
    - groundedness 校验器按同一约定统计无出处事实句占比（§8.4）。
"""

from agent.retrieval.search import Hit

# 注入上下文的资料块头部与内容
RAG_SYSTEM_PROMPT = (
    "你是一个严谨的中文学习助理。请**仅根据下面编号的资料**回答用户问题：\n"
    "1. 每个来自资料的事实性论断，句末必须标注来源编号，如 [1] 或 [1][3]；\n"
    "2. 资料不足以回答的部分，明确说「资料中未提及」，不要编造；\n"
    "3. 通用客套话、总结性引导语无需标注编号。\n"
    "回答保持简洁、结构清晰。"
)

NO_CONTEXT_SYSTEM_PROMPT = (
    "你是一个乐于助人的中文 AI 助理。回答简洁准确，不确定时如实说明。\n"
    "（当前问题没有命中的知识库资料，若问题需要资料支撑，请如实告知用户先上传相关文档。）"
)

# 引用不足时的重写提示（§8.4：针对性补充指令，只重写一次）
REWRITE_SYSTEM_PROMPT = (
    "你上一版回答里有事实性论断没有标注来源编号。请重新回答用户问题，"
    "严格遵守：**每个来自资料的事实性论断，句末都标注来源编号 [n]**；"
    "资料未提及的内容明确说明。"
)


def build_context_blocks(hits: list[Hit]) -> str:
    """把命中父块拼成带编号的资料区。

    格式：
        [1] 来源：《机器学习基础》（第 3 段）
        父块正文……

    编号 n 与 build_citations 的 n 同源（Hit.rank），提示词与脚注/落库三处一致。
    """
    parts: list[str] = []
    for h in hits:
        page = f"（第 {h.page} 段）" if h.page is not None else ""
        parts.append(f"[{h.rank}] 来源：《{h.document_title}》{page}\n{h.parent_content}")
    return "\n\n".join(parts)
