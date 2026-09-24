"""知识库入库管线（M2-8，§8.1）。

    文件 → 解析 → 去噪 → 父子分块 → jieba 分词 → 批量 embedding → 落库

幂等与断点续传（§7.6）：
    - 分块是确定性的：同一份文件（content_hash 相同）重复执行得到完全
      相同的 (chunk_type, ord) 序列；
    - 父块 INSERT ... ON CONFLICT DO NOTHING；
    - 子块先查库里已有的 ord（已带 embedding），只对缺失的批次调用
      embedding，逐批落库 —— 任务中途失败后重跑，已嵌入的批次不再
      重复计费（断点续传），DB 的 UNIQUE(document_id, chunk_type, ord)
      兜底并发重复执行；
    - 失败：status='failed' + error_message + retry_count+1，
      由 /kb/documents/{id}/reingest 手动重跑（C5）。

worker 走 app_worker 角色（BYPASSRLS）：文档入库是后台合法跨租户写入，
写入仍显式携带行自带的 user_id（双保险习惯，与仓储层一致）。
"""

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import text

from agent.provider.embedding import EmbeddingClient, EmbeddingError
from agent.retrieval.denoise import remove_running_heads
from agent.retrieval.parser import ParseError, parse_bytes
from agent.retrieval.splitter import split_parent_child
from apps.api.core.config import settings
from worker.celery_app import celery_app
from worker.db import session_scope

logger = logging.getLogger(__name__)


def _build_embed_client() -> EmbeddingClient:
    """独立成函数：测试用 fake 替换（celery eager + fake client 跑全链路）。"""
    if not settings.embed_configured:
        raise EmbeddingError(
            "EMBED_* 未配置：无法向量化（检查 .env 的 EMBED_BASE_URL/API_KEY/MODEL）"
        )
    return EmbeddingClient(
        base_url=settings.embed_base_url,
        api_key=settings.embed_api_key,
        model=settings.embed_model,
        dim=settings.embed_dim,
        batch_size=settings.embed_batch_size,
        max_retries=settings.embed_max_retries,
    )


def _jieba_tokens(content: str) -> str:
    import jieba

    return " ".join(jieba.lcut(content))


_INSERT_PARENT = text("""
    INSERT INTO chunks (document_id, user_id, chunk_type, ord, content, content_tokens, meta)
    VALUES (:did, :uid, 'parent', :ord, :content, :tokens, CAST(:meta AS JSONB))
    ON CONFLICT (document_id, chunk_type, ord) DO NOTHING
    RETURNING id
""")

_INSERT_CHILD = text("""
    INSERT INTO chunks (document_id, user_id, parent_id, chunk_type, ord,
                        content, content_tokens, meta, embedding)
    VALUES (:did, :uid, :parent_id, 'child', :ord, :content, :tokens,
            CAST(:meta AS JSONB), CAST(:embedding AS vector))
    ON CONFLICT (document_id, chunk_type, ord) DO NOTHING
""")


@celery_app.task(name="worker.tasks.ingest.ingest_document", bind=True)
def ingest_document(self, document_id: str) -> dict[str, Any]:
    """入库任务。返回 {"status": "ready"|"failed", ...}；状态以 documents 表为准。"""
    try:
        return _run_ingest(document_id)
    except Exception as exc:  # noqa: BLE001 —— 管线内任何失败都要落到 documents.status
        logger.exception("ingest failed doc=%s", document_id)
        _mark_failed(document_id, str(exc)[:500])
        return {"status": "failed", "document_id": document_id, "error": str(exc)[:500]}


def _run_ingest(document_id: str) -> dict[str, Any]:
    # ---- 1) 读文档记录 ----
    with session_scope() as session:
        row = session.execute(
            text("SELECT id, user_id, status, meta FROM documents WHERE id = :did"),
            {"did": document_id},
        ).mappings().first()
        if row is None:
            raise ParseError(f"文档记录不存在：{document_id}")
        doc = dict(row)
    meta = doc["meta"] or {}
    kind = meta.get("kind", "text")
    suffix = Path(meta.get("filename", "document.bin")).suffix or ".bin"
    path = Path(settings.upload_dir) / str(doc["user_id"]) / f"{document_id}{suffix}"

    # ---- 2) 解析（§13.7：worker 子进程内执行，与 API 隔离）----
    data = path.read_bytes()
    parsed = parse_bytes(
        data, kind,
        max_pages=settings.parse_max_pages,
        max_unzip_mb=settings.parse_max_unzip_mb,
    )
    pages = remove_running_heads(parsed.pages)

    # ---- 3) 分块 + 分词 ----
    chunks = split_parent_child(
        pages,
        parent_tokens=settings.chunk_parent_tokens,
        child_tokens=settings.chunk_child_tokens,
        overlap_ratio=settings.chunk_overlap_ratio,
    )
    if not chunks:
        raise ParseError("解析后没有可检索的文本内容（空文档或全为空白）")
    parents = [c for c in chunks if c.chunk_type == "parent"]
    children = [c for c in chunks if c.chunk_type == "child"]
    tokens_by_content: dict[str, str] = {}

    def tokens_for(c) -> str:
        if c.content not in tokens_by_content:
            tokens_by_content[c.content] = _jieba_tokens(c.content)
        return tokens_by_content[c.content]

    # ---- 4) 父块落库（幂等）----
    parent_ids: dict[int, str] = {}
    with session_scope() as session:
        for c in parents:
            r = session.execute(
                _INSERT_PARENT,
                {"did": document_id, "uid": str(doc["user_id"]), "ord": c.ord,
                 "content": c.content, "tokens": tokens_for(c),
                 "meta": json.dumps(c.meta, ensure_ascii=False)},
            ).scalar_one_or_none()
            if r is not None:
                parent_ids[c.ord] = str(r)
        # ON CONFLICT DO NOTHING 时 RETURNING 为空 → 补查已有 id
        missing = [c.ord for c in parents if c.ord not in parent_ids]
        for ord_ in missing:
            r = session.execute(
                text("""
                    SELECT id FROM chunks
                    WHERE document_id = :did AND chunk_type = 'parent' AND ord = :ord
                """),
                {"did": document_id, "ord": ord_},
            ).scalar_one()
            parent_ids[ord_] = str(r)

    # ---- 5) 子块：断点续传（已有 embedding 的 ord 跳过）----
    with session_scope() as session:
        existing_ords = {
            int(r) for r in session.execute(
                text("""
                    SELECT ord FROM chunks
                    WHERE document_id = :did AND chunk_type = 'child'
                      AND embedding IS NOT NULL
                """),
                {"did": document_id},
            ).scalars()
        }
    todo = [c for c in children if c.ord not in existing_ords]
    logger.info(
        "ingest doc=%s parser=%s parents=%d children=%d todo=%d",
        document_id, parsed.parser_used, len(parents), len(children), len(todo),
    )

    embed = _build_embed_client()

    def persist_batch(start_idx: int, vectors: list[list[float]]) -> None:
        batch = todo[start_idx : start_idx + len(vectors)]
        with session_scope() as session:
            for c, vec in zip(batch, vectors, strict=True):
                session.execute(
                    _INSERT_CHILD,
                    {"did": document_id, "uid": str(doc["user_id"]),
                     "parent_id": parent_ids[c.meta["parent_ord"]],
                     "ord": c.ord, "content": c.content, "tokens": tokens_for(c),
                     "meta": json.dumps(c.meta, ensure_ascii=False),
                     "embedding": json.dumps(vec)},
                )

    embed.embed_all(
        [c.content for c in todo],
        on_batch=persist_batch,
    )

    # ---- 6) 完成 ----
    with session_scope() as session:
        session.execute(
            text("""
                UPDATE documents
                SET status = 'ready', parser_used = :parser, chunk_count = :cnt,
                    error_message = NULL, updated_at = now()
                WHERE id = :did
            """),
            {"parser": parsed.parser_used, "cnt": len(chunks), "did": document_id},
        )
    return {
        "status": "ready",
        "document_id": document_id,
        "parents": len(parents),
        "children": len(children),
        "embedded_now": len(todo),
        "parser": parsed.parser_used,
    }


def _mark_failed(document_id: str, message: str) -> None:
    try:
        with session_scope() as session:
            session.execute(
                text("""
                    UPDATE documents
                    SET status = 'failed', error_message = :msg,
                        retry_count = retry_count + 1, updated_at = now()
                    WHERE id = :did
                """""),
                {"msg": message, "did": document_id},
            )
    except Exception:  # noqa: BLE001 —— 标记失败本身不能再抛（celery 日志兜底）
        logger.exception("mark failed failed doc=%s", document_id)
