"""会话与历史接口（M1）：列表 / 消息历史（游标分页）。

契约（§11.2）：
    GET /api/v1/conversations                    → 自己的会话列表（B3）
    GET /api/v1/conversations/{id}/messages      → 会话内消息（B4 隔离验证点）

隔离语义（B4）：请求他人的 conversation id 时，RLS + 仓储过滤后查不到行，
返回 404 —— 刻意不返回 403，避免向调用方泄露「该 id 存在但属于别人」。
"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from apps.api.core.db import tenant_session
from apps.api.core.deps import UserCtx, current_user
from apps.api.core.errors import AppError, ErrorCode
from apps.api.repositories import conversations as conv_repo

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("")
async def list_conversations(
    request: Request,
    user: Annotated[UserCtx, Depends(current_user)],
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, description="上一页返回的 next_cursor"),
) -> dict:
    """当前用户的会话列表（游标分页）。"""
    trace_id = getattr(request.state, "trace_id", "")
    cursor_created_at = None
    cursor_id = None
    if cursor:
        try:
            cursor_created_at, cursor_id = cursor.split("|", 1)
        except ValueError as exc:
            raise AppError(ErrorCode.VALIDATION, "无效的游标", 422) from exc

    async with tenant_session(user.id) as session:
        items = await conv_repo.list_conversations(
            session, user.id,
            limit=limit,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
        )
    next_cursor = None
    if len(items) == limit:
        last = items[-1]
        next_cursor = f"{last['created_at'].isoformat()}|{last['id']}"
    return {
        "data": {
            "items": [
                {
                    "id": str(it["id"]),
                    "title": it["title"],
                    "last_message_at": it["last_message_at"].isoformat() if it["last_message_at"] else None,
                    "created_at": it["created_at"].isoformat(),
                }
                for it in items
            ],
            "next_cursor": next_cursor,
        },
        "trace_id": trace_id,
    }


@router.get("/{conversation_id}/messages")
async def list_messages(
    conversation_id: UUID,
    request: Request,
    user: Annotated[UserCtx, Depends(current_user)],
    limit: int = Query(default=50, ge=1, le=200),
    cursor: int | None = Query(default=None, ge=0, description="上一页最后一条的 seq"),
) -> dict:
    """会话内消息（seq 升序）。

    B4：他人的 conversation_id → 404（不泄露存在性）；会话不存在同样 404。
    """
    trace_id = getattr(request.state, "trace_id", "")
    async with tenant_session(user.id) as session:
        conv = await conv_repo.get_conversation(session, user.id, conversation_id)
        if conv is None:
            # RLS + 仓储过滤后查不到：不区分「不存在」与「不属于你」
            raise AppError(ErrorCode.NOT_FOUND, "会话不存在", 404)
        messages = await conv_repo.list_messages(
            session, user.id, conversation_id, limit=limit, cursor_seq=cursor
        )
    return {
        "data": {
            "conversation": {
                "id": str(conv["id"]),
                "title": conv["title"],
                "created_at": conv["created_at"].isoformat(),
            },
            "items": [
                {
                    "id": str(m["id"]),
                    "seq": m["seq"],
                    "role": m["role"],
                    "content": m["content"],
                    "status": m["status"],
                    "created_at": m["created_at"].isoformat(),
                }
                for m in messages
            ],
        },
        "trace_id": trace_id,
    }
