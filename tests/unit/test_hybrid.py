"""混合检索纯函数单测（M6-1/M6-2）：RRF 数学 与 tsquery 净化。

这两块是「易错且在集成环境难以精准断言」的逻辑：
    - RRF：同分并列的稳定性、多路命中只计一次、payload 合并；
    - tsquery：元字符注入（`& | ! ( ) : *` 会改变查询语义或直接语法错误）、
      停用词、空查询 —— 任一漏掉都会让关键词路静默失效或行为异常。
"""

import pytest

from agent.retrieval.hybrid import build_tsquery, rrf_fuse, tokenize


class TestTokenize:
    def test_chinese_tokens(self) -> None:
        tokens = tokenize("监督学习和无监督学习的区别")
        # jieba 会切出「监督」「学习」这类词；停用词（和/的）被丢弃
        assert "监督" in tokens
        assert "学习" in tokens
        assert "和" not in tokens
        assert "的" not in tokens

    def test_dedup_preserves_order(self) -> None:
        tokens = tokenize("监督学习 监督学习 无监督")
        assert tokens.count("监督") == 1
        assert tokens[0] == "监督"

    def test_strips_punctuation_and_operators(self) -> None:
        tokens = tokenize("监督学习 & | ! (RAG) : * 'x'")
        joined = "".join(tokens)
        for ch in "&|!():*'\"":
            assert ch not in joined
        assert "RAG" in tokens  # 英文保留

    def test_empty_and_stopword_only(self) -> None:
        assert tokenize("") == []
        assert build_tsquery("的了是和与") == ""


class TestBuildTsquery:
    def test_or_join(self) -> None:
        tsq = build_tsquery("监督学习")
        assert " | " in tsq
        assert tsq.count("|") == len(tsq.split(" | ")) - 1

    def test_injection_neutralized(self) -> None:
        """tsquery 元字符必须被剔除：否则 `a & b` 会变成 AND，`!x` 变成取反。"""
        tsq = build_tsquery("监督 & 学习 | !无监督")
        assert "&" not in tsq and "!" not in tsq and "|" in tsq
        # 结构仍是合法 OR 串：token 之间恰好是 |
        parts = [p.strip() for p in tsq.split("|")]
        assert all(parts) and len(parts) >= 2

    def test_max_tokens(self) -> None:
        tsq = build_tsquery("监" * 100, max_tokens=3)
        assert tsq.count("|") <= 2

    def test_no_meta_chars_in_output(self) -> None:
        for query in ["a:b", "(x)", "'y'", "a<->b", "c\\d", "e*f"]:
            tsq = build_tsquery(query)
            for ch in "()<>:*'\"\\":
                assert ch not in tsq, f"{query!r} → {tsq!r} 仍含元字符 {ch!r}"


class TestRrfFuse:
    def _c(self, cid: str, score: float = 1.0, extra: dict | None = None) -> dict:
        return {"chunk_id": cid, "score": score, **(extra or {})}

    def test_score_formula(self) -> None:
        """score = Σ 1/(k+rank)：单路第 1 名 = 1/(60+1)。"""
        fused = rrf_fuse({"vector": [self._c("a")]}, k=60)
        assert fused[0].rrf_score == pytest.approx(1 / 61)

    def test_multi_path_accumulates(self) -> None:
        """两路都命中的候选分数更高（这是 RRF 提升相关性的原理）。"""
        fused = rrf_fuse(
            {"vector": [self._c("a"), self._c("b")], "keyword": [self._c("b"), self._c("a")]},
            k=60,
        )
        by_id = {c.chunk_id: c for c in fused}
        assert by_id["a"].rrf_score == pytest.approx(1 / 61 + 1 / 62)
        assert by_id["b"].rrf_score == pytest.approx(1 / 62 + 1 / 61)

    def test_rank_recorded_per_path(self) -> None:
        fused = rrf_fuse(
            {"vector": [self._c("a"), self._c("b")], "keyword": [self._c("c")]}, k=60
        )
        by_id = {c.chunk_id: c for c in fused}
        assert by_id["a"].vector_rank == 1 and by_id["a"].keyword_rank is None
        assert by_id["c"].sources == ["keyword"]
        assert by_id["b"].sources == ["vector"]

    def test_dedupe_within_path(self) -> None:
        """同一路里重复出现的 chunk 只按最好名次计一次（防分数虚高）。"""
        fused = rrf_fuse({"vector": [self._c("a"), self._c("a")]}, k=60)
        assert len(fused) == 1
        assert fused[0].rrf_score == pytest.approx(1 / 61)

    def test_stable_order_on_tie(self) -> None:
        """同分按首次出现顺序 —— 保证评测可复现（无随机抖动）。

        构造真实同分：vector 的第 2 名（b）与 keyword 的第 2 名（d）都是 1/(60+2)。
        """
        fused = rrf_fuse(
            {"vector": [self._c("a"), self._c("b")], "keyword": [self._c("c"), self._c("d")]},
            k=60,
        )
        tie = [c.chunk_id for c in fused if c.rrf_score == pytest.approx(1 / 62)]
        assert tie == ["b", "d"]  # b 先在 vector 出现 → 排在 d 前

    def test_limit_truncates(self) -> None:
        rows = [self._c(f"c{i}") for i in range(10)]
        assert len(rrf_fuse({"vector": rows}, k=60, limit=3)) == 3

    def test_payload_merge_keeps_richer(self) -> None:
        """两路命中时保留字段更全的 payload（标题/正文只在其中一路有值时特别重要）。"""
        fused = rrf_fuse(
            {
                "vector": [{"chunk_id": "a", "score": 0.9}],
                "keyword": [{"chunk_id": "a", "score": 0.2, "title": "T", "content": "C"}],
            },
            k=60,
        )
        assert fused[0].payload.get("title") == "T"
        assert fused[0].payload.get("score") is not None

    def test_empty_paths(self) -> None:
        assert rrf_fuse({"vector": [], "keyword": []}) == []
        assert rrf_fuse({}) == []

    def test_invalid_k(self) -> None:
        with pytest.raises(ValueError):
            rrf_fuse({"vector": [self._c("a")]}, k=0)
