"""检索层指标单元测试（确定性逻辑必须零噪声）。"""

from evals.metrics import aggregate, evaluate_retrieval


class TestEvaluateRetrieval:
    def test_perfect_ranking(self) -> None:
        m = evaluate_retrieval(["d1", "d2"], {"d1": 2, "d2": 1}, k=10)
        assert m.recall_at_k == 1.0
        assert m.mrr_at_k == 1.0
        assert m.ndcg_at_k == 1.0

    def test_partial_recall(self) -> None:
        m = evaluate_retrieval(["x", "d1"], {"d1": 2, "d2": 1}, k=10)
        assert m.recall_at_k == 0.5
        assert m.mrr_at_k == 0.5  # 首个相关在第 2 位

    def test_nothing_relevant(self) -> None:
        m = evaluate_retrieval(["x", "y"], {"d1": 2}, k=10)
        assert m.recall_at_k == 0.0
        assert m.ndcg_at_k == 0.0

    def test_k_truncation(self) -> None:
        m = evaluate_retrieval(["x", "y", "d1"], {"d1": 2}, k=2)
        assert m.recall_at_k == 0.0
        m2 = evaluate_retrieval(["x", "y", "d1"], {"d1": 2}, k=3)
        assert m2.recall_at_k == 1.0

    def test_graded_beats_binary(self) -> None:
        """rel=2 排在前面应比 rel=1 排前面得分高（nDCG 的意义）。"""
        m_high = evaluate_retrieval(["d1", "d2"], {"d1": 2, "d2": 1}, k=10)
        m_low = evaluate_retrieval(["d2", "d1"], {"d1": 2, "d2": 1}, k=10)
        assert m_high.ndcg_at_k > m_low.ndcg_at_k
        # recall/MRR 不区分分级
        assert m_high.recall_at_k == m_low.recall_at_k
        assert m_high.mrr_at_k == m_low.mrr_at_k

    def test_empty_wanted(self) -> None:
        m = evaluate_retrieval(["d1"], {}, k=10)
        assert m.recall_at_k == 0.0


class TestAggregate:
    def test_mean(self) -> None:
        ms = [
            evaluate_retrieval(["d1"], {"d1": 2}, k=10),
            evaluate_retrieval(["x", "d1"], {"d1": 2}, k=10),
        ]
        agg = aggregate(ms)
        assert agg["recall@10"]["mean"] == 1.0
        assert agg["mrr@10"]["mean"] == 0.75
        assert agg["n"]["count"] == 2
