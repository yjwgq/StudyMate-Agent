"""知识库仓储（documents 表，§6.5）。

与 conversations 仓储同一约定：RLS 已过滤他人行，查询仍显式带
user_id 条件作为第一道防线（双保险，ADR-9）。
"""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_COLS = "id, user_id, title, source_type, status, content_hash, parser_used, chunk_count, error_message, meta, created_at"


async def create_document(
    session: AsyncSession,
    *,
    user_id: UUID,
    title: str,
    content_hash: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """建 processing 记录。UNIQUE(user_id, content_hash) 兜底并发重复上传。"""
    row = await session.execute(
        text(f"""
            INSERT INTO documents (user_id, title, source_type, content_hash, meta)
            VALUES (:uid, :title, 'upload', :hash, CAST(:meta AS JSONB))
            ON CONFLICT (user_id, content_hash) DO NOTHING
            RETURNING {_COLS}
        """),
        {"uid": str(user_id), "title": title, "hash": content_hash,
         "meta": _json(meta)},
    )
    r = row.mappings().first()
    if r is None:
        # 并发窗口内另一请求已插入：按去重语义返回已有行
        return await get_document_by_hash(session, user_id, content_hash)  # type: ignore[return-value]
    return dict(r)


async def get_document_by_hash(
    session: AsyncSession, user_id: UUID, content_hash: str
) -> dict[str, Any] | None:
    row = await session.execute(
        text(f"""
            SELECT {_COLS} FROM documents
            WHERE user_id = :uid AND content_hash = :hash
        """),
        {"uid": str(user_id), "hash": content_hash},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def get_document(
    session: AsyncSession, user_id: UUID, document_id: UUID
) -> dict[str, Any] | None:
    row = await session.execute(
        text(f"""
            SELECT {_COLS} FROM documents WHERE id = :did AND user_id = :uid
        """),
        {"did": str(document_id), "uid": str(user_id)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def list_documents(
    session: AsyncSession, user_id: UUID, *, limit: int = 50
) -> list[dict[str, Any]]:
    row = await session.execute(
        text(f"""
            SELECT {_COLS} FROM documents
            WHERE user_id = :uid
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"uid": str(user_id), "limit": limit},
    )
    return [dict(r) for r in row.mappings().all()]


async def count_documents(session: AsyncSession, user_id: UUID) -> int:
    row = await session.execute(
        text("SELECT count(*) FROM documents WHERE user_id = :uid"),
        {"uid": str(user_id)},
    )
    return int(row.scalar_one())


async def delete_document(
    session: AsyncSession, user_id: UUID, document_id: UUID
) -> None:
    """落盘失败时回收 processing 记录（级联清 chunks 由 FK 处理）。"""
    await session.execute(
        text("DELETE FROM documents WHERE id = :did AND user_id = :uid"),
        {"did": str(document_id), "uid": str(user_id)},
    )


async def reset_document(session: AsyncSession, document_id: UUID) -> None:
    """重跑 ingest 前置：状态回 processing、错误清零（reingest，C5）。"""
    await session.execute(
        text("""
            UPDATE documents
            SET status = 'processing', error_message = NULL, updated_at = now()
            WHERE id = :did
        """),
        {"did": str(document_id)},
    )


def _json(obj: dict[str, Any]) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
