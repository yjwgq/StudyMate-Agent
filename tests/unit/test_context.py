"""检索上下文拼装单元测试（M3-3）：编号一致性 —— 提示词 [n] 与 citations.n 同源。"""

from agent.retrieval.context import build_context_blocks
from agent.retrieval.search import Hit, build_citations


def _hit(n: int, title: str = "机器学习基础") -> Hit:
    return Hit(
        rank=n,
        child_chunk_id=None,
        parent_chunk_id=None,
        document_id=None,
        document_title=title,
        score=0.9 - 0.1 * n,
        child_content=f"子块摘要内容 {n}",
        parent_content=f"父块正文内容 {n}" * 30,
        page=n + 2,
    )


class TestBuildContextBlocks:
    def test_numbering_matches_rank(self) -> None:
        blocks = build_context_blocks([_hit(1), _hit(2)])
        assert "[1] 来源：《机器学习基础》（第 3 段）" in blocks
        assert "[2] 来源：《机器学习基础》（第 4 段）" in blocks

    def test_page_omitted_when_none(self) -> None:
        h = _hit(1)
        h.page = None
        assert "（第" not in build_context_blocks([h])

    def test_empty(self) -> None:
        assert build_context_blocks([]) == ""


class TestBuildCitations:
    def test_structure_matches_design(self) -> None:
        """§8.4：{n, chunk_id, document_id, title, snippet, rerank_score}。"""
        h = _hit(1)
        h.child_chunk_id = h.parent_chunk_id = h.document_id = None
        cite = build_citations([h])[0]
        assert set(cite) >= {"n", "document_id", "title", "snippet", "rerank_score"}
        assert cite["rerank_score"] is None  # M3 无精排，M6 填充
        assert cite["n"] == 1

    def test_snippet_truncated(self) -> None:
        h = _hit(1)
        h.child_content = "长" * 500
        cite = build_citations([h])[0]
        assert len(cite["snippet"]) <= 201  # 200 + 省略号
