"""父子分块器单测（M2-5）。"""

from agent.retrieval.parser import ParsedPage
from agent.retrieval.splitter import approx_tokens, split_parent_child


def _doc(lines_per_page=20, pages=3, line="这是一行用于测试的中文文本内容"):
    return [
        ParsedPage(page_no=p + 1, text="\n".join(line for _ in range(lines_per_page)))
        for p in range(pages)
    ]


def test_approx_tokens_cjk_vs_ascii():
    assert approx_tokens("四字以上") == 4          # CJK 每字 1 token
    assert approx_tokens("abcdefgh") == 2          # 8 字符 / 4
    assert approx_tokens("") == 0


def test_basic_structure_parent_and_child():
    chunks = split_parent_child(_doc())
    parents = [c for c in chunks if c.chunk_type == "parent"]
    children = [c for c in chunks if c.chunk_type == "child"]
    assert parents and children
    # ord 各自从 0 连续
    assert [p.ord for p in parents] == list(range(len(parents)))
    assert [c.ord for c in children] == list(range(len(children)))
    # 子块引用父块
    parent_ords = {p.ord for p in parents}
    assert all(c.meta["parent_ord"] in parent_ords for c in children)
    # 父块不带 embedding 责任（内容存在、页码合理）
    assert all(p.content.strip() for p in parents)
    assert all(1 <= p.meta["page_start"] <= 3 for p in parents)


def test_child_tokens_within_budget():
    chunks = split_parent_child(
        _doc(lines_per_page=40, pages=5, line="字" * 30)  # 大文档
    )
    children = [c for c in chunks if c.chunk_type == "child"]
    assert len(children) > 1
    for c in children:
        assert approx_tokens(c.content) <= 256 + 40  # 单行硬切的容差


def test_overlap_present_between_children():
    line = "重叠检测行" + "甲乙丙丁" * 10
    chunks = split_parent_child(
        [ParsedPage(page_no=1, text="\n".join(line for _ in range(60)))]
    )
    children = [c for c in chunks if c.chunk_type == "child"]
    assert len(children) >= 2
    # 前一块结尾行出现在下一块开头（重叠 10%）
    tail = children[0].content.splitlines()[-1]
    assert tail in children[1].content.splitlines()


def test_short_doc_single_parent():
    chunks = split_parent_child([ParsedPage(page_no=1, text="只有一行")])
    parents = [c for c in chunks if c.chunk_type == "parent"]
    children = [c for c in chunks if c.chunk_type == "child"]
    assert len(parents) == 1 and len(children) == 1
    assert children[0].content == parents[0].content


def test_empty_doc_returns_empty():
    assert split_parent_child([ParsedPage(page_no=1, text="   \n  ")]) == []


def test_deterministic_for_same_input():
    a = split_parent_child(_doc())
    b = split_parent_child(_doc())
    assert [(c.chunk_type, c.ord, c.content) for c in a] == [
        (c.chunk_type, c.ord, c.content) for c in b
    ]
