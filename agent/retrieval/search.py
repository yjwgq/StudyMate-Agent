"""混合检索 + 精排（M6，§8.2 / §8.3）。

管线（设计文档 §8.2）：

    query
     → 并行双路召回（每路 top N，ADR-2 为多租户过滤召回损失留余量）
          · 向量路：embedding cosine (<=>) + user_id 过滤 + hnsw.iterative_scan
          · 关键词路：tsv (ts_rank_cd)；查询侧 jieba 分词（与入库侧同一分词器）
     → RRF 融合（k=60）去重 → 候选 ~50
     → Reranker 精排 → top 6
     → 父块回溯（parent_id）→ 注入上下文

降级矩阵（§8.3，全部显式）：
    · 向量路失败/超时  → 仅关键词路，标记 `vector`
    · 关键词路失败/超时 → 仅向量路，标记 `keyword`
    · 两路都挂         → 无检索结果，标记 `retrieval`（上层已有该语义）
    · 精排失败/超时/未配置 → 用 RRF 顺序，标记 `rerank`
_feature flag（§14.3，agent/retrieval/flags.py）：
    hybrid.enabled=false → 完全走 M3 单路向量（G1 的对照管线，行为保持一致）
    rerank.enabled=false → 跳过精排

上一里程碑（M3）的单路实现保留在 `_search_vector_only`：它既是对照管线，
也是混合检索的向量路实现本体（同一段 SQL，保证两套管线可比）。
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from uuid import UUID

from openai import AsyncOpenAI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent.retrieval.flags import RetrievalFlags, get_flags
from agent.retrieval.hybrid import build_tsquery, rrf_fuse
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
    score: float = 0.0            # 最终排序分（精排分优先，否则向量余弦分）
    rerank_score: float | None = None  # M6：精排分（未精排/关键词路命中时为 None）
    vector_score: float | None = None  # 向量余弦分（调试台逐路对比用）
    keyword_score: float | None = None  # ts_rank_cd（调试台逐路对比用）
    rrf_score: float | None = None     # RRF 融合分
    sources: list[str] = field(default_factory=list)  # 命中来源：vector / keyword
    child_content: str = ""       # 引用脚注摘要用
    parent_content: str = ""      # 注入上下文用
    page: int | None = None       # 子块 meta.page（若有）
    metadata: dict = field(default_factory=dict)


@dataclass
class SearchOutcome:
    """一次检索的完整结果（M6：把降级与逐路诊断带出检索层）。"""

    hits: list[Hit] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)  # §8.3 降级标记
    diagnostics: dict = field(default_factory=dict)    # 逐路分数与耗时（调试台/归因）
    elapsed_ms: int = 0
    flags: dict = field(default_factory=dict)

    @property
    def has_hits(self) -> bool:
        return bool(self.hits)


# ---------------- 召回 SQL（两路共用父块回溯） ----------------

_VECTOR_SQL = """
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

# 关键词路：tsv 生成列（M2 已建 GIN 索引）+ jieba 查询词。
# ts_rank_cd 与 BM25 不同（无文档长度归一化、无 k1/b）—— ADR-2 明确不声称 BM25。
_KEYWORD_SQL = """
    SELECT c.id, c.parent_id, c.document_id, c.content, c.meta,
           d.title,
           ts_rank_cd(c.tsv, q.query) AS score
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    CROSS JOIN (SELECT to_tsquery('simple', :tsq) AS query) AS q
    WHERE c.user_id = :uid
      AND c.chunk_type = 'child'
      AND d.status = 'ready'
      AND c.tsv @@ q.query
    ORDER BY score DESC
    LIMIT :k
"""

_PARENT_SQL = """
    SELECT id, content, meta
    FROM chunks
    WHERE user_id = :uid AND id = ANY(CAST(:pids AS uuid[]))
"""


class Retriever:
    """无状态检索器（进程级单例由 get_retriever() 提供）。"""

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

    # ---------------- 两路召回（各自独立超时 → 独立降级） ----------------

    async def _vector_recall(
        self, session: AsyncSession, user_id: UUID, qvec: list[float], k: int
    ) -> list[dict]:
        vec_literal = "[" + ",".join(f"{x:.7f}" for x in qvec) + "]"
        rows = await session.execute(
            text(_VECTOR_SQL), {"qvec": vec_literal, "uid": str(user_id), "k": k}
        )
        return [dict(r) for r in rows.mappings().all()]

    async def _keyword_recall(
        self, session: AsyncSession, user_id: UUID, query: str, k: int
    ) -> list[dict]:
        tsq = build_tsquery(query)
        if not tsq:
            return []
        rows = await session.execute(
            text(_KEYWORD_SQL), {"tsq": tsq, "uid": str(user_id), "k": k}
        )
        return [dict(r) for r in rows.mappings().all()]

    async def _fetch_parents(
        self, session: AsyncSession, user_id: UUID, parent_ids: list[UUID]
    ) -> dict:
        if not parent_ids:
            return {}
        rows = await session.execute(
            text(_PARENT_SQL),
            {"uid": str(user_id), "pids": [str(p) for p in parent_ids]},
        )
        return {r["id"]: (r["content"] or "") for r in rows.mappings().all()}

    # ---------------- 精排（可选增强，失败即降级） ----------------

    async def _rerank(
        self, query: str, candidates: list[dict], flags: RetrievalFlags, degraded: list[str]
    ) -> list[dict]:
        """对候选精排；失败/未启用时原样返回（调用方按既有顺序继续）。"""
        from agent.provider.rerank import RerankError, get_rerank_client

        if not flags.rerank:
            degraded.append("rerank")
            return candidates
        client = get_rerank_client()
        if client is None:
            degraded.append("rerank")
            return candidates
        docs = [_candidate_text(c) for c in candidates]
        try:
            hits = await client.rerank(query, docs, top_n=settings.rerank_top_n)
        except RerankError as exc:
            logger.warning("精排降级（%s），使用 RRF 顺序", exc)
            degraded.append("rerank")
            return candidates

        by_index = {c["id"]: c for c in candidates}
        ordered: list[dict] = []
        for h in hits:
            if 0 <= h.index < len(candidates):
                cand = dict(candidates[h.index])
                cand["_rerank_score"] = h.score
                ordered.append(cand)
        if not ordered:
            degraded.append("rerank")
            return candidates
        # 精排未覆盖到的候选（top_n 截断）接在后面 —— 保证召回不因精排变少
        chosen = {c["id"] for c in ordered}
        ordered.extend(c for c in candidates if c["id"] not in chosen)
        del by_index
        return ordered

    # ---------------- 主管线 ----------------

    async def search(
        self,
        session: AsyncSession,
        user_id: UUID,
        query: str,
        *,
        qvec: list[float] | None = None,
        top_k: int | None = None,
        max_contexts: int | None = None,
        flags: RetrievalFlags | None = None,
        redis=None,
    ) -> SearchOutcome:
        """一次检索：按 flag 决定走混合还是单路，返回带降级标记的结果。

        qvec：预计算的查询向量。调用方（chat）应在**事务外**先 embed，
        避免在 DB 事务里等 embedding 网络往返占死连接池。
        """
        k = top_k or settings.retrieval_top_k_children
        limit = max_contexts or settings.retrieval_max_contexts
        started = time.perf_counter()
        flags = flags or await get_flags(redis)
        degraded: list[str] = []
        diag: dict = {
            "flags": flags.as_dict(),
            "vector_ms": None,
            "keyword_ms": None,
            "rerank_ms": None,
            "vector_count": 0,
            "keyword_count": 0,
            "fused_count": 0,
        }

        if not flags.hybrid:
            # 对照管线：M3 单路（行为与 M3 完全一致，G1 的 baseline）
            hits = await self._search_vector_only(
                session, user_id, query, qvec=qvec, k=k, limit=limit, flags=flags, degraded=degraded,
                diag=diag, started=started,
            )
        else:
            hits = await self._search_hybrid(
                session, user_id, query, qvec=qvec, limit=limit, flags=flags,
                degraded=degraded, diag=diag, started=started,
            )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "retrieval uid=%s q=%r hits=%d degraded=%s elapsed=%dms "
            "(vector=%s keyword=%s fused=%s rerank=%sms)",
            user_id, query[:40], len(hits), degraded, elapsed_ms,
            diag["vector_count"], diag["keyword_count"], diag["fused_count"], diag["rerank_ms"],
        )
        return SearchOutcome(
            hits=hits, degraded=degraded, diagnostics=diag,
            elapsed_ms=elapsed_ms, flags=flags.as_dict(),
        )

    async def _search_vector_only(
        self, session, user_id, query, *, qvec, k, limit, flags, degraded, diag, started
    ) -> list[Hit]:
        """M3 单路向量检索（对照管线 + 混合检索的向量路实现本体）。

        注意 `flags.vector` 的语义：它模拟**向量依赖不可用**（§8.3 / G3），
        对单路管线意味着「没有任何可用召回」——这正是要对比的那个数字
        （单路 0 召回 vs 混合走关键词兜底）。M6 实测：漏掉这个判断会让
        故障演练静默失效（对比表出现 delta=0 的假象）。
        """
        if not flags.vector:
            degraded.append("vector")
            diag["vector_count"] = 0
            return []
        try:
            if qvec is None:
                t0 = time.perf_counter()
                qvec = await self.embed_query(query)
                diag["vector_ms"] = int((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            children = await asyncio.wait_for(
                self._vector_recall(session, user_id, qvec, k),
                timeout=settings.retrieval_vector_timeout_s,
            )
            diag["vector_ms"] = int((time.perf_counter() - t0) * 1000)
            diag["vector_count"] = len(children)
        except TimeoutError:
            logger.warning("向量路超时（>%ss），无可用召回", settings.retrieval_vector_timeout_s)
            degraded.append("vector")
            return []
        except Exception as exc:  # noqa: BLE001 —— embedding/DB 异常都算向量路故障
            logger.warning("向量路失败，降级：%s", exc)
            degraded.append("vector")
            return []

        best = _best_child_per_parent(children)
        return await self._assemble(
            session, user_id, list(best.values()), limit=limit, flags=flags,
            degraded=degraded, rerank_query=None,
        )

    async def _search_hybrid(
        self, session, user_id, query, *, qvec, limit, flags, degraded, diag, started
    ) -> list[Hit]:
        """混合检索：向量路 ‖ 关键词路 → RRF → 精排 → 父块回溯。"""
        k = settings.hybrid_top_k

        async def vector_path() -> list[dict]:
            if not flags.vector:
                degraded.append("vector")
                return []
            vec = qvec
            try:
                if vec is None:
                    t0 = time.perf_counter()
                    vec = await self.embed_query(query)
                    diag["vector_ms"] = int((time.perf_counter() - t0) * 1000)
                t0 = time.perf_counter()
                rows = await asyncio.wait_for(
                    self._vector_recall(session, user_id, vec, k),
                    timeout=settings.retrieval_vector_timeout_s,
                )
                diag["vector_ms"] = int((time.perf_counter() - t0) * 1000)
                diag["vector_count"] = len(rows)
                return rows
            except TimeoutError:
                logger.warning("向量路超时（>%ss）→ 仅关键词路", settings.retrieval_vector_timeout_s)
                degraded.append("vector")
                return []
            except Exception as exc:  # noqa: BLE001
                logger.warning("向量路失败（%s）→ 仅关键词路", exc)
                degraded.append("vector")
                return []

        async def keyword_path() -> list[dict]:
            if not flags.keyword:
                degraded.append("keyword")
                return []
            try:
                t0 = time.perf_counter()
                # **独立连接**：一条 AsyncSession 会把并发查询串行化
                # （M6 实测：共用 session 时两路耗时相加 930ms ≈ 471+474），
                # 独立 tenant_session 各自持连接，才是真并行。
                from apps.api.core.db import tenant_session as _tenant_session

                async with _tenant_session(user_id) as kw_session:
                    rows = await asyncio.wait_for(
                        self._keyword_recall(kw_session, user_id, query, k),
                        timeout=settings.retrieval_keyword_timeout_s,
                    )
                diag["keyword_ms"] = int((time.perf_counter() - t0) * 1000)
                diag["keyword_count"] = len(rows)
                return rows
            except TimeoutError:
                logger.warning("关键词路超时（>%ss）→ 仅向量路", settings.retrieval_keyword_timeout_s)
                degraded.append("keyword")
                return []
            except Exception as exc:  # noqa: BLE001 —— tsquery 语法/DB 异常
                logger.warning("关键词路失败（%s）→ 仅向量路", exc)
                degraded.append("keyword")
                return []

        vector_rows, keyword_rows = await asyncio.gather(vector_path(), keyword_path())

        if not vector_rows and not keyword_rows:
            degraded.append("retrieval")
            return []

        # RRF 融合（k=60）—— 只看排名，免归一化（向量分与 ts_rank_cd 量纲不同）
        fused = rrf_fuse(
            {"vector": vector_rows, "keyword": keyword_rows},
            k=settings.rrf_k,
            id_key="id",
            limit=settings.rrf_candidates,
        )
        diag["fused_count"] = len(fused)
        candidates = [
            {
                **c.payload,
                "id": c.chunk_id,
                "rrf_score": c.rrf_score,
                "sources": c.sources,
                "vector_rank": c.vector_rank,
                "keyword_rank": c.keyword_rank,
                # 各路分数分别取（两路都命中时 payload 只留了一路）
                "_vector_score": c.path_scores.get("vector"),
                "_keyword_score": c.path_scores.get("keyword"),
            }
            for c in fused
        ]

        # 精排（可选增强；失败已由 _rerank 内部标记降级）
        t0 = time.perf_counter()
        ranked = await self._rerank(query, candidates, flags, degraded)
        diag["rerank_ms"] = int((time.perf_counter() - t0) * 1000)

        best = _best_child_per_parent(ranked, keep_rank=True)
        return await self._assemble(
            session, user_id, list(best.values()), limit=limit, flags=flags,
            degraded=degraded, rerank_query=None,
        )

    # ---------------- 父块回溯与组装 ----------------

    async def _assemble(
        self, session, user_id, children: list[dict], *, limit, flags, degraded, rerank_query
    ) -> list[Hit]:
        parent_ids = [c["parent_id"] or c["id"] for c in children][:limit]
        parent_content = await self._fetch_parents(session, user_id, parent_ids)

        hits: list[Hit] = []
        for c in children[:limit]:
            pid = c["parent_id"] or c["id"]
            meta = c.get("meta") or {}
            parent_text = parent_content.get(pid, "")
            if len(parent_text) > settings.retrieval_parent_max_chars:
                parent_text = parent_text[: settings.retrieval_parent_max_chars] + "…"
            rerank_score = c.get("_rerank_score")
            vector_score = c.get("_vector_score")
            if vector_score is None and "sources" not in c:
                # 单路管线：score 即向量余弦分
                vector_score = c.get("score")
            hits.append(
                Hit(
                    rank=len(hits) + 1,
                    child_chunk_id=c["id"],
                    parent_chunk_id=pid,
                    document_id=c["document_id"],
                    document_title=c["title"],
                    score=round(float(rerank_score if rerank_score is not None else c["score"]), 6),
                    rerank_score=round(float(rerank_score), 6) if rerank_score is not None else None,
                    vector_score=round(float(vector_score), 6) if vector_score is not None else None,
                    keyword_score=(
                        round(float(c["_keyword_score"]), 6)
                        if c.get("_keyword_score") is not None else None
                    ),
                    rrf_score=round(float(c["rrf_score"]), 6) if c.get("rrf_score") is not None else None,
                    sources=list(c.get("sources") or (["vector"] if flags.hybrid is False else [])),
                    child_content=c["content"],
                    parent_content=parent_text,
                    page=meta.get("page") if isinstance(meta, dict) else None,
                    metadata=meta if isinstance(meta, dict) else {},
                )
            )
        return hits


def _candidate_text(c: dict) -> str:
    """精排输入文本：候选的子块内容（精排针对召回单元，父块在回溯阶段提供上下文）。"""
    return str(c.get("content") or "")


def _best_child_per_parent(children: list[dict], *, keep_rank: bool = False) -> dict:
    """每个父块只保留一个子块（保持传入顺序 = 已有排序）。

    keep_rank=False（单路）：children 已按向量距离升序，首次出现即最优；
    keep_rank=True（混合）：children 已按精排（或 RRF）排序，首次出现即最优。
    """
    best: dict = {}
    for c in children:
        pid = c["parent_id"] or c["id"]
        if pid not in best:
            best[pid] = c
    return best


def build_citations(hits: list[Hit]) -> list[dict]:
    """把命中结果转成 messages.citations 的 JSONB 结构（§8.4）。

    M6 起 rerank_score 有值（未精排时为 None，供 UI 判断是否显示精排分）。
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
                "rerank_score": h.rerank_score,
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
