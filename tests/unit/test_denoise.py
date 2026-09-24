"""页眉页脚去噪单测（M2-4）。"""

from agent.retrieval.denoise import normalize_for_repeat, remove_running_heads
from agent.retrieval.parser import ParsedPage


def _pages_with_header_footer(n_pages=6):
    pages = []
    for p in range(1, n_pages + 1):
        body = "\n".join(f"第{p}页正文第{i}段" for i in range(5))
        pages.append(
            ParsedPage(
                page_no=p,
                text=f"StudyMate 学习助手\n- {p} -\n{body}\nStudyMate 学习助手\n- {p} -",
            )
        )
    return pages


def test_normalize_merges_page_numbers():
    assert normalize_for_repeat("第 3 页") == normalize_for_repeat("第 17 页")
    assert normalize_for_repeat("- 3 -") == normalize_for_repeat("- 9 -")


def test_removes_repeated_edges():
    pages = _pages_with_header_footer()
    cleaned = remove_running_heads(pages)
    assert len(cleaned) == len(pages)
    for page in cleaned:
        assert "StudyMate" not in page.text
        assert "正文第1段" in page.text          # 正文保留
    # 原始入参不被修改
    assert "StudyMate" in pages[0].text


def test_short_doc_untouched():
    pages = [ParsedPage(page_no=1, text="页眉\n正文"), ParsedPage(page_no=2, text="页眉\n正文")]
    cleaned = remove_running_heads(pages)   # < 3 页不统计
    assert cleaned == pages


def test_distinct_bodies_not_removed():
    pages = [
        ParsedPage(page_no=p, text=f"标题{p}\n" + "\n".join(f"正文{p}-{i}" for i in range(4)))
        for p in range(1, 6)
    ]
    cleaned = remove_running_heads(pages)
    for page in cleaned:
        assert f"正文{page.page_no}" in page.text   # 各页不同的行不误删
