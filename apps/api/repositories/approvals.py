"""审批仓储（M5-2，§6.7 / §7.3）。

状态机：pending → approved / rejected（决策）
              approved → executed（工具真实执行后）
              pending → expired（1h TTL，决策时惰性判定）
所有操作经 tenant_session（RLS 表）；thread_id = conversation_id，
resume 时用它定位 LangGraph checkpoint。
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def create_approval(
    session: AsyncSession,
    *,
    user_id: UUID,
    thread_id: str,
    tool_name: str,
    args: dict[str, Any],
    risk_level: int,
    reason: str = "",
) -> dict[str, Any]:
    row = await session.execute(
        text("""
            INSERT INTO approvals (user_id, thread_id, tool_call, risk_level)
            VALUES (:uid, :thread, CAST(:tool_call AS JSONB), :risk)
            RETURNING id, status, created_at, expires_at
        """),
        {
            "uid": str(user_id),
            "thread": thread_id,
            "tool_call": json.dumps(
                {"tool": tool_name, "args": args, "reason": reason}, ensure_ascii=False
            ),
            "risk": risk_level,
        },
    )
    return dict(row.mappings().one())


async def get_approval(session: AsyncSession, user_id: UUID, approval_id: UUID) -> dict[str, Any] | None:
    row = await session.execute(
        text("""
            SELECT id, thread_id, tool_call, risk_level, status, expires_at, exec_result, created_at
            FROM approvals WHERE id = :aid AND user_id = :uid
        """),
        {"aid": str(approval_id), "uid": str(user_id)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def list_pending(session: AsyncSession, user_id: UUID, *, limit: int = 20) -> list[dict[str, Any]]:
    """待审批列表；过期的 pending 在读取时惰性置 expired。"""
    await session.execute(
        text("""
            UPDATE approvals SET status = 'expired', updated_at = now()
            WHERE user_id = :uid AND status = 'pending' AND expires_at < now()
        """),
        {"uid": str(user_id)},
    )
    row = await session.execute(
        text("""
            SELECT id, thread_id, tool_call, risk_level, created_at, expires_at
            FROM approvals
            WHERE user_id = :uid AND status = 'pending'
            ORDER BY created_at ASC LIMIT :limit
        """),
        {"uid": str(user_id), "limit": limit},
    )
    return [dict(r) for r in row.mappings().all()]


async def decide_approval(
    session: AsyncSession, user_id: UUID, approval_id: UUID, *, approved: bool
) -> dict[str, Any] | None:
    """决策：pending → approved/rejected。返回更新后的行；非 pending 返回 None。"""
    new_status = "approved" if approved else "rejected"
    row = await session.execute(
        text("""
            UPDATE approvals
            SET status = :status, decided_at = now(), updated_at = now()
            WHERE id = :aid AND user_id = :uid AND status = 'pending' AND expires_at >= now()
            RETURNING id, thread_id, tool_call, risk_level, status
        """),
        {"aid": str(approval_id), "uid": str(user_id), "status": new_status},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def check_approved(
    session: AsyncSession, user_id: UUID, approval_id: UUID
) -> dict[str, Any] | None:
    """恢复执行时校验：必须是 approved 状态（防伪造 approved_approval_id）。"""
    row = await session.execute(
        text("""
            SELECT id, tool_call, risk_level FROM approvals
            WHERE id = :aid AND user_id = :uid AND status = 'approved'
        """),
        {"aid": str(approval_id), "uid": str(user_id)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def mark_executed(
    session: AsyncSession, user_id: UUID, approval_id: UUID, result: dict[str, Any]
) -> None:
    await session.execute(
        text("""
            UPDATE approvals
            SET status = 'executed', executed_at = now(), exec_result = CAST(:res AS JSONB),
                updated_at = now()
            WHERE id = :aid AND user_id = :uid
        """),
        {"aid": str(approval_id), "uid": str(user_id),
         "res": json.dumps(result, ensure_ascii=False, default=str)[:2000]},
    )


async def find_reusable(
    session: AsyncSession, user_id: UUID, *, thread_id: str, tool_name: str, args: dict[str, Any]
) -> dict[str, Any] | None:
    """查找可复用的审批（同一 thread + 工具 + 同参数，未过期，pending/approved）。

    为什么需要它（M5 验收实锤）：LangGraph 在 resume 时**从头重跑节点**，
    L2 拦截会再次触发 —— 若每次都新建行，一次审批会留下多行孤儿 pending
    （重复审批记录 + 面板出现幽灵卡片）。幂等复用是正确语义：
      - 已有 approved → 直接执行（不再 interrupt）
      - 已有 pending  → 复用同一 id 再 interrupt（等用户决策）
    JSONB 相等比较天然忽略 key 顺序（无需额外 canonical 化）。
    """
    row = await session.execute(
        text("""
            SELECT id, status, tool_call, risk_level FROM approvals
            WHERE user_id = :uid AND thread_id = :thread
              AND tool_call->>'tool' = :tool
              AND tool_call->'args' = CAST(:args AS JSONB)
              AND status IN ('pending', 'approved')
              AND expires_at >= now()
            ORDER BY created_at DESC LIMIT 1
        """),
        {
            "uid": str(user_id),
            "thread": thread_id,
            "tool": tool_name,
            "args": json.dumps(args, ensure_ascii=False, sort_keys=True),
        },
    )
    r = row.mappings().first()
    return dict(r) if r else None
