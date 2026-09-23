"""pgvector 单路检索（M3-1 / M3-2，§8.2，ADR-2）。

这是 M3 的检索基线（单路向量），M6 会在此基础上加混合检索 + RRF + Rerank，
对照评测（G1）依赖本模块保持接口稳定。

要点：
    - cosine 距离：pgvector 的 `<=>`；HNSW 索引 + `hnsw.iterative_scan =
      iterative_scan`（pgvector ≥ 0.8）—— 带过滤条件时普通 HNSW 会因
      「先取 top-k 再过滤」丢结果，iterative_scan 在图遍历中持续补页，
      这是 ADR-2 选 pgvector 的关键前提；
    - 租户隔离双保险：tenant_session 的 RLS（SET LOCAL app.user_id）之上，
      SQL 里再显式 `user_id = :uid`（ADR-9 第 2 条，防御 RLS 策略回退）；
    - 子块召回 → parent_id 回溯父块 → 按父块去重（多个子块命中同一父块时
      保留最高分），注入上下文的是父块全文 —— 子块用于精准召回，
      父块提供完整语境（§8.2 / §6.5）；
    - query embedding 用 AsyncOpenAI（DashScope 兼容模式），维度运行时校验。
"""

import logging
import time
from dataclasses import dataclass, field
from uuid import UUID

from openai import AsyncOpenAI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class Hit:
    """一次命中的完整信息：召回靠子块，上下文靠父块。"""

    rank: int = 0
    child_chunk_id: UUID | None = None
    parent_chunk_id: UUID | None = None
    document_id: UUID | None = None
    document_title: str = ""
    score: float = 0.0            # cosine similarity（1 - <=> 距离）
    child_content: str = ""       # 引用脚注摘要用
    parent_content: str = ""      # 注入上下文用
    page: int | None = None       # 子块 meta.page（若有）
    metadata: dict = field(default_factory=dict)


class Retriever:
    """无状态检索器：每次查询 = 1 次 embedding + 2 条 SQL（召回 + 父块回溯）。"""

    def __init__(self) -> None:
        self._embedder: AsyncOpenAI | None = None

    def _get_embedder(self) -> AsyncOpenAI:
        if self._embedder is None:
            self._embedder = AsyncOpenAI(
                base_url=settings.embed_base_url,
                api_key=settings.embed_api_key,
                timeout=10.0,
            )
        return self._embedder

    async def embed_query(self, query: str) -> list[float]:
        """查询向量化。维度与入库端（EMBED_DIM）不一致时立即失败 ——
        两端维度漂移意味着检索结果全错，宁可显式报错。"""
        resp = await self._get_embedder().embeddings.create(
            model=settings.embed_model, input=[query]
        )
        vec = resp.data[0].embedding
        if len(vec) != settings.embed_dim:
            raise ValueError(
                f"query embedding 维度 {len(vec)} != EMBED_DIM {settings.embed_dim}"
            )
        return vec

    async def search(
        self,
        session: AsyncSession,
        user_id: UUID,
        query: str,
        *,
        qvec: list[float] | None = None,
        top_k: int | None = None,
        max_contexts: int | None = None,
    ) -> list[Hit]:
        """单路向量检索：子块召回 top_k → 父块回溯去重 → 最多 max_contexts 条。

        会话必须处于事务内（tenant_session）—— RLS 依赖事务。
        qvec：预计算的查询向量。调用方（chat）应在**事务外**先 embed，
        避免在 DB 事务里等 embedding 网络往返占死连接池；
        省略时（eval/调试台）在事务内现算，方便但别在高并发路径用。
        """
        k = top_k or settings.retrieval_top_k_children
        limit = max_contexts or settings.retrieval_max_contexts
        started = time.perf_counter()

        if qvec is None:
            qvec = await self.embed_query(query)
        vec_literal = "[" + ",".join(f"{x:.7f}" for x in qvec) + "]"

        # ---- 1) 子块召回 ----
        # （iterative_scan GUC 在引擎连接层统一设置，见 core/db.py _on_connect）
        children = (
            await session.execute(
                text(
                    """
                    SELECT c.id, c.parent_id, c.document_id, c.content, c.meta,
                           d.title,
                           1 - (c.embedding <=> CAST(:qvec AS vector)) AS score
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE c.user_id = :uid
                      AND c.chunk_type = 'child'
                      AND c.embedding IS NOT NULL
                      AND d.status = 'ready'
                    ORDER BY c.embedding <=> CAST(:qvec AS vector)
                    LIMIT :k
                    """
                ),
                {"qvec": vec_literal, "uid": str(user_id), "k": k},
            )
        ).mappings().all()

        if not children:
            return []

        # ---- 2) 父块回溯 + 去重（每个父块只保留最高分子块）----
        best_by_parent: dict[UUID, dict] = {}
        order_by_parent: dict[UUID, int] = {}
        for i, row in enumerate(children):
            pid = row["parent_id"]
            if pid is None:
                # 理论不发生（子块必有父块，DDL 约束）；防御性处理：用子块自己
                pid = row["id"]
            if pid not in best_by_parent:
                best_by_parent[pid] = dict(row)
                order_by_parent[pid] = i

        parent_ids = list(best_by_parent.keys())
        parents = (
            await session.execute(
                text(
                    """
                    SELECT id, content, meta
                    FROM chunks
                    WHERE user_id = :uid AND id = ANY(CAST(:pids AS uuid[]))
                    """
                ),
                {"uid": str(user_id), "pids": [str(p) for p in parent_ids]},
            )
        ).mappings().all()
        parent_content = {p["id"]: (p["content"] or "") for p in parents}

        # ---- 3) 组装：按最高分排序，截断父块长度 ----
        hits: list[Hit] = []
        for pid in sorted(
            best_by_parent,
            key=lambda p: -float(best_by_parent[p]["score"]),
        )[:limit]:
            best = best_by_parent[pid]
            meta = best["meta"] or {}
            parent_text = parent_content.get(pid, "")
            if len(parent_text) > settings.retrieval_parent_max_chars:
                parent_text = parent_text[: settings.retrieval_parent_max_chars] + "…"
            hits.append(
                Hit(
                    rank=len(hits) + 1,
                    child_chunk_id=best["id"],
                    parent_chunk_id=pid,
                    document_id=best["document_id"],
                    document_title=best["title"],
                    score=round(float(best["score"]), 6),
                    child_content=best["content"],
                    parent_content=parent_text,
                    page=meta.get("page") if isinstance(meta, dict) else None,
                    metadata=meta if isinstance(meta, dict) else {},
                )
            )

        logger.info(
            "retrieval uid=%s q=%r hits=%d/%d elapsed=%.0fms",
            user_id, query[:40], len(hits), len(children),
            (time.perf_counter() - started) * 1000,
        )
        return hits


def build_citations(hits: list[Hit]) -> list[dict]:
    """把命中结果转成 messages.citations 的 JSONB 结构（§8.4）。

    rerank_score 预留给 M6 精排；M3 只有向量分。
    """
    out: list[dict] = []
    for h in hits:
        snippet = h.child_content
        if len(snippet) > settings.retrieval_snippet_max_chars:
            snippet = snippet[: settings.retrieval_snippet_max_chars] + "…"
        out.append(
            {
                "n": h.rank,
                "chunk_id": str(h.child_chunk_id) if h.child_chunk_id else None,
                "parent_chunk_id": str(h.parent_chunk_id) if h.parent_chunk_id else None,
                "document_id": str(h.document_id) if h.document_id else None,
                "title": h.document_title,
                "page": h.page,
                "snippet": snippet,
                "score": h.score,
                "rerank_score": None,
            }
        )
    return out


# 模块级单例：Retriever 无状态，只持有 embedder 客户端
_retriever: Retriever | None = None


def get_retriever() -> Retriever:
    global _retriever
    if _retriever is None:
        _retriever = Retriever()
    return _retriever
