"""页眉页脚去噪（M2-4，§8.1）。

原理：页眉/页脚的本质特征是「跨页重复」——同一行文本（或仅页码不同）
出现在大部分页面的顶部/底部。因此按页统计边缘行的重复度，出现率
超过阈值的视为页眉/页脚并移除。

页码变体（如「第 3 页」「- 3 -」「Page 3」）把数字归一成 # 后再比对，
避免同一页眉因页码变化而统计不上。
"""

import re

_DIGITS_RE = re.compile(r"\d+")


def normalize_for_repeat(line: str) -> str:
    """归一化：去首尾空白 + 数字替换为 #（页码变体归并）。"""
    return _DIGITS_RE.sub("#", line.strip())


def remove_running_heads(
    pages,
    *,
    edge_lines: int = 2,
    min_page_ratio: float = 0.5,
    min_pages: int = 3,
):
    """移除出现在 ≥ min_page_ratio 页面顶部/底部的重复行。

    pages: list[ParsedPage]（parser.ParsedPage）。少于 min_pages 页时样本
    不可靠，原样返回。返回新的页面列表（不修改入参）。
    """
    if len(pages) < min_pages:
        return pages

    total = len(pages)

    def _dominant(edge: str) -> set[str]:
        """统计顶部/底部边缘行的重复度，返回应移除的归一化行集合。"""
        counter: dict[str, int] = {}
        for page in pages:
            lines = [line for line in page.text.splitlines() if line.strip()]
            picked = lines[:edge_lines] if edge == "top" else lines[-edge_lines:]
            seen_in_page: set[str] = set()  # 同页重复只计一次
            for line in picked:
                key = normalize_for_repeat(line)
                if key and key not in seen_in_page:
                    seen_in_page.add(key)
                    counter[key] = counter.get(key, 0) + 1
        return {k for k, n in counter.items() if n / total >= min_page_ratio}

    remove_top = _dominant("top")
    remove_bottom = _dominant("bottom")
    if not remove_top and not remove_bottom:
        return pages

    from agent.retrieval.parser import ParsedPage

    cleaned: list = []
    for page in pages:
        lines = page.text.splitlines()
        # 顶部：从头部开始剥掉命中页眉的行；底部同理（只处理非空行的边缘）
        top_cut = 0
        stripped = 0
        for line in lines:
            if not line.strip():
                top_cut += 1
                continue
            if stripped >= edge_lines:
                break
            if normalize_for_repeat(line) in remove_top:
                top_cut += 1
                stripped += 1
            else:
                break
        bottom_cut = 0
        stripped = 0
        for line in reversed(lines):
            if not line.strip():
                bottom_cut += 1
                continue
            if stripped >= edge_lines:
                break
            if normalize_for_repeat(line) in remove_bottom:
                bottom_cut += 1
                stripped += 1
            else:
                break
        kept = lines[top_cut : len(lines) - bottom_cut] if bottom_cut else lines[top_cut:]
        cleaned.append(ParsedPage(page_no=page.page_no, text="\n".join(kept)))
    return cleaned
