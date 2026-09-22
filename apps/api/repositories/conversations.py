"""会话与消息仓储（§6.4）。

所有查询都在 tenant_session 内执行：RLS 已经把「别人的行」过滤掉，
这里再显式带 user_id 条件作为第一道防线（双保险，ADR-9）。
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------- conversations ----------------

async def create_conversation(
    session: AsyncSession, user_id: UUID, title: str | None = None
) -> dict[str, Any]:
    row = await session.execute(
        text("""
            INSERT INTO conversations (user_id, title)
            VALUES (:uid, :title)
            RETURNING id, user_id, title, created_at, last_message_at
        """),
        {"uid": str(user_id), "title": title},
    )
    return dict(row.mappings().one())


async def get_conversation(
    session: AsyncSession, user_id: UUID, conversation_id: UUID
) -> dict[str, Any] | None:
    row = await session.execute(
        text("""
            SELECT id, user_id, title, last_message_at, created_at
            FROM conversations
            WHERE id = :cid AND user_id = :uid
        """),
        {"cid": str(conversation_id), "uid": str(user_id)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def list_conversations(
    session: AsyncSession,
    user_id: UUID,
    *,
    limit: int = 20,
    cursor_created_at: str | None = None,
    cursor_id: str | None = None,
) -> list[dict[str, Any]]:
    """游标分页（§11.1）：按 created_at DESC, id DESC 排序，稳定且不漂移。"""
    conditions = "WHERE user_id = :uid"
    params: dict[str, Any] = {"uid": str(user_id), "limit": limit}
    if cursor_created_at and cursor_id:
        conditions += " AND (created_at, id) < (:ts, :cid)"
        params["ts"] = cursor_created_at
        params["cid"] = cursor_id
    row = await session.execute(
        text(f"""
            SELECT id, title, last_message_at, created_at
            FROM conversations
            {conditions}
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
        """),
        params,
    )
    return [dict(r) for r in row.mappings().all()]


async def touch_last_message(session: AsyncSession, conversation_id: UUID) -> None:
    await session.execute(
        text("UPDATE conversations SET last_message_at = now() WHERE id = :cid"),
        {"cid": str(conversation_id)},
    )


# ---------------- messages ----------------

async def next_seq(session: AsyncSession, conversation_id: UUID) -> int:
    """会话内递增序号。调用方必须持有会话锁（§7.4），串行化保证不跳号。"""
    row = await session.execute(
        text("""
            SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE conversation_id = :cid
        """),
        {"cid": str(conversation_id)},
    )
    return int(row.scalar_one())


async def insert_message(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
    seq: int,
    role: str,
    content: str | None,
    status: str = "completed",
    token_usage: dict[str, Any] | None = None,
    trace_id: str | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = await session.execute(
        text("""
            INSERT INTO messages
              (conversation_id, user_id, seq, role, content, status, token_usage, trace_id, error)
            VALUES
              (:cid, :uid, :seq, :role, :content, :status,
               CAST(:usage AS JSONB), :trace, CAST(:err AS JSONB))
            RETURNING id, seq, created_at
        """),
        {
            "cid": str(conversation_id),
            "uid": str(user_id),
            "seq": seq,
            "role": role,
            "content": content,
            "status": status,
            "usage": None if token_usage is None else json.dumps(token_usage),
            "trace": trace_id,
            "err": None if error is None else json.dumps(error, ensure_ascii=False),
        },
    )
    return dict(row.mappings().one())


async def list_messages(
    session: AsyncSession,
    user_id: UUID,
    conversation_id: UUID,
    *,
    limit: int = 50,
    cursor_seq: int | None = None,
) -> list[dict[str, Any]]:
    """按 seq 升序返回（正序渲染历史）。游标 = 最后一条的 seq。"""
    conditions = "WHERE conversation_id = :cid AND user_id = :uid"
    params: dict[str, Any] = {
        "cid": str(conversation_id),
        "uid": str(user_id),
        "limit": limit,
    }
    if cursor_seq is not None:
        conditions += " AND seq > :cseq"
        params["cseq"] = cursor_seq
    row = await session.execute(
        text(f"""
            SELECT id, seq, role, content, status, citations, token_usage,
                   created_at
            FROM messages
            {conditions}
            ORDER BY seq ASC
            LIMIT :limit
        """),
        params,
    )
    return [dict(r) for r in row.mappings().all()]
