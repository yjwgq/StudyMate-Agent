"""父子分块（§6.5，M2-5）。

策略：small-to-big —— 子块 256 token 用于精准召回，命中后回溯父块
1024 token 喂模型；子块重叠 10%。

实现约定：
    - 「token」是估算值：CJK 字符每字约 1 token，其余每 4 字符约 1 token
      （§1.3 不引入本地 tokenizer；估算足以支撑分块尺寸控制）；
    - 切分以「行」为最小单位（不撕裂词与句子），重叠也以整行为单位：
      前一块结尾的若干行整体作为下一块的开头，直到估算 token 覆盖重叠量；
    - 页码随行携带，块的 meta 记录 page_start / page_end（M3 引用定位用）；
    - ord 在同类型内全文档连续编号（0..n-1），与 DDL 的
      UNIQUE(document_id, chunk_type, ord) 对应，也是断点续传的坐标。
"""

from dataclasses import dataclass, field


def approx_tokens(text: str) -> int:
    """估算 token 数：CJK ≈ 1 token/字；其他 ≈ 1 token/4 字符。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f")
    other = len(text) - cjk
    return cjk + -(-other // 4)  # ceil


@dataclass
class Line:
    """一行文本及其来源页（1-based）。"""

    text: str
    page_no: int
    tokens: int = field(default=-1)  # 惰性计算


@dataclass
class PlannedChunk:
    """分块产物：待落库的块（不含 embedding）。"""

    chunk_type: str          # 'parent' | 'child'
    ord: int
    content: str
    meta: dict


def _line_tokens(line: Line) -> int:
    if line.tokens < 0:
        line.tokens = approx_tokens(line.text)
    return line.tokens


def _collect_pages_to_lines(pages) -> list[Line]:
    lines: list[Line] = []
    for page in pages:
        for raw in page.text.splitlines():
            t = raw.strip()
            if t:
                lines.append(Line(text=t, page_no=page.page_no))
    return lines


def _split_by_budget(lines: list[Line], budget: int) -> list[list[Line]]:
    """把行序列按 token 预算切成段；超预算的单行硬切（保留完整性优先级最低）。"""
    segments: list[list[Line]] = []
    cur: list[Line] = []
    cur_tokens = 0
    for line in lines:
        lt = _line_tokens(line)
        if cur and cur_tokens + lt > budget:
            segments.append(cur)
            cur, cur_tokens = [], 0
        if lt > budget and not cur:
            # 单行超预算：按字符硬切成 budget 对应的近似长度
            char_budget = max(budget, 1)
            for i in range(0, len(line.text), char_budget):
                seg = Line(text=line.text[i : i + char_budget], page_no=line.page_no)
                seg.tokens = approx_tokens(seg.text)
                segments.append([seg])
            continue
        cur.append(line)
        cur_tokens += lt
    if cur:
        segments.append(cur)
    return segments or [[]]


def _overlap_tail(segment: list[Line], overlap_tokens: int) -> list[Line]:
    """取段末尾若干行作为重叠尾巴（token 估算不超过 overlap_tokens）。"""
    tail: list[Line] = []
    used = 0
    for line in reversed(segment):
        lt = _line_tokens(line)
        if used + lt > overlap_tokens:
            break
        tail.insert(0, line)
        used += lt
    return tail


def split_parent_child(
    pages,
    *,
    parent_tokens: int = 1024,
    child_tokens: int = 256,
    overlap_ratio: float = 0.1,
) -> list[PlannedChunk]:
    """去噪后的页面列表 → 父子两路块。

    返回父块与子块的平铺列表（先父后子，调用方按 chunk_type 分批落库）。
    空文档返回空列表（调用方据此把文档标记为 failed：无可检索内容）。
    """
    lines = _collect_pages_to_lines(pages)
    if not lines:
        return []

    parent_segs = _split_by_budget(lines, parent_tokens)
    child_overlap = int(child_tokens * overlap_ratio)

    chunks: list[PlannedChunk] = []
    child_ord = 0
    for p_ord, seg in enumerate(parent_segs):
        parent_meta = {
            "page_start": seg[0].page_no,
            "page_end": seg[-1].page_no,
        }
        chunks.append(
            PlannedChunk(
                chunk_type="parent",
                ord=p_ord,
                content="\n".join(line.text for line in seg),
                meta=parent_meta,
            )
        )
        # 父块内部切子块，块间带重叠尾巴
        cursor: list[Line] = list(seg)
        while cursor:
            sub_segs = _split_by_budget(cursor, child_tokens)
            first = sub_segs[0]
            chunks.append(
                PlannedChunk(
                    chunk_type="child",
                    ord=child_ord,
                    content="\n".join(line.text for line in first),
                    meta={
                        "page_start": first[0].page_no,
                        "page_end": first[-1].page_no,
                        "parent_ord": p_ord,
                    },
                )
            )
            child_ord += 1
            if len(sub_segs) == 1:
                break
            rest = sub_segs[1:]
            # 重叠：下一子块以「上一子块结尾行 + 剩余内容」重新聚合
            cursor = _overlap_tail(first, child_overlap) + [line for s in rest for line in s]
    return chunks
