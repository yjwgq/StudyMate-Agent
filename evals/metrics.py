"""检索层指标（M3-6，§14.2.1）。

双层指标的第一层：Recall@k / MRR@k / nDCG@k —— 纯确定性计算，
不含 LLM judge（judge 独立性与噪声地板见 §14.2.3/4，M7 落地答案层指标）。

相关性分级（golden 标注口径）：
    2 = 应命中（relevant，标准答案明确指向该文档）
    1 = 相关（partially relevant，同主题可接受）
    0 = 无关（不可接受）

nDCG 用 2^rel - 1 折损；Recall@k 只看 rel >= 1；MRR 取首个相关命中位置。
"""

from dataclasses import dataclass


@dataclass
class RetrievalMetrics:
    recall_at_k: float
    mrr_at_k: float
    ndcg_at_k: float
    k: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            f"recall@{self.k}": round(self.recall_at_k, 4),
            f"mrr@{self.k}": round(self.mrr_at_k, 4),
            f"ndcg@{self.k}": round(self.ndcg_at_k, 4),
        }


def dcg(relevances: list[int]) -> float:
    """DCG：rel=0 的位置贡献 0，等价于只累计相关位置。"""
    return sum(rel / (1.0 if i == 0 else (i + 1) ** 0.5)
               for i, rel in enumerate(relevances))


def evaluate_retrieval(
    ranked_doc_ids: list[str],
    relevant: dict[str, int],
    k: int = 10,
) -> RetrievalMetrics:
    """单条 query 的检索指标。

    Args:
        ranked_doc_ids: 检索结果按排名的 doc_id 列表（父块已归并到 doc 维度）。
        relevant: {doc_id: 分级相关性}，来自 golden set。
        k: 截断位置。
    """
    top = ranked_doc_ids[:k]
    top_rels = [relevant.get(d, 0) for d in top]

    # Recall@k：应命中文档（rel>=1）出现在 top-k 的比例
    wanted = [d for d, rel in relevant.items() if rel >= 1]
    hit = [d for d in wanted if d in top]
    recall = len(hit) / len(wanted) if wanted else 0.0

    # MRR@k：首个相关（rel>=1）结果的倒数排名
    mrr = 0.0
    for i, rel in enumerate(top_rels):
        if rel >= 1:
            mrr = 1.0 / (i + 1)
            break

    # nDCG@k：分级折损（rel ∈ {0,1,2}）
    actual = dcg(top_rels)
    ideal = dcg(sorted(relevant.values(), reverse=True)[:k])
    ndcg = actual / ideal if ideal > 0 else 0.0

    return RetrievalMetrics(recall_at_k=recall, mrr_at_k=mrr, ndcg_at_k=ndcg, k=k)


def aggregate(metrics: list[RetrievalMetrics]) -> dict[str, dict[str, float | int]]:
    """多条 query 聚合：各指标取均值，附样本数。"""
    if not metrics:
        return {}
    n = len(metrics)
    out: dict[str, dict[str, float | int]] = {"n": {"count": n}}
    k = metrics[0].k
    out[f"recall@{k}"] = {"mean": round(sum(m.recall_at_k for m in metrics) / n, 4)}
    out[f"mrr@{k}"] = {"mean": round(sum(m.mrr_at_k for m in metrics) / n, 4)}
    out[f"ndcg@{k}"] = {"mean": round(sum(m.ndcg_at_k for m in metrics) / n, 4)}
    return out
