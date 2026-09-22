"""认证仓储：users 与 refresh_tokens（§6.3）。

refresh token 轮转与重放检测：
    - 登录：创建新 family，写入 token 哈希；
    - 刷新：FOR UPDATE 行锁串行化并发刷新 →
        已撤销 → 判定凭据泄露，撤销整个 family（TOKEN_REUSE_DETECTED）；
        未撤销且未过期 → 旧 token 置 revoked(rotated)，签发新 token（同 family）；
    - 登出：撤销给定 token。

安全注记：users / refresh_tokens 不受 RLS 保护（注册、登录、刷新发生在
「拿到用户身份之前」，无法预设 app.user_id），因此本模块每个查询都以
显式参数化条件圈定范围 —— 不允许出现无 WHERE 的全表访问。
"""

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.security import hash_refresh_token

# ---------------- users ----------------

async def get_user_by_email(session: AsyncSession, email: str) -> dict[str, Any] | None:
    row = await session.execute(
        text("""
            SELECT id, email, password_hash, display_name, role, settings, is_active
            FROM users WHERE email = :email
        """),
        {"email": email},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def get_user_by_id(session: AsyncSession, user_id: UUID) -> dict[str, Any] | None:
    row = await session.execute(
        text("""
            SELECT id, email, password_hash, display_name, role, settings, is_active
            FROM users WHERE id = :uid
        """),
        {"uid": str(user_id)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    display_name: str | None,
) -> dict[str, Any]:
    row = await session.execute(
        text("""
            INSERT INTO users (email, password_hash, display_name)
            VALUES (:email, :ph, :dn)
            RETURNING id, email, display_name, role, is_active, created_at
        """),
        {"email": email, "ph": password_hash, "dn": display_name},
    )
    return dict(row.mappings().one())


async def touch_last_login(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        text("UPDATE users SET last_login_at = now() WHERE id = :uid"),
        {"uid": str(user_id)},
    )


# ---------------- refresh tokens ----------------

async def issue_refresh_token(
    session: AsyncSession,
    user_id: UUID,
    family_id: UUID,
    ttl_days: int,
) -> tuple[str, datetime]:
    """签发 refresh token（不透明随机串，库中只存 sha256）。"""
    raw = secrets.token_urlsafe(48)
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    await session.execute(
        text("""
            INSERT INTO refresh_tokens (user_id, token_hash, family_id, expires_at)
            VALUES (:uid, :th, :fid, :exp)
        """),
        {
            "uid": str(user_id),
            "th": hash_refresh_token(raw),
            "fid": str(family_id),
            "exp": expires_at,
        },
    )
    return raw, expires_at


async def new_family() -> UUID:
    return uuid4()


async def lock_token_by_hash(
    session: AsyncSession, raw_token: str
) -> dict[str, Any] | None:
    """按哈希取 refresh token 并加行锁（FOR UPDATE）。

    行锁把「并发用同一 token 刷新」串行化：后到者一定看到 revoked=true，
    触发重放检测，而不是双写两条新 token。
    """
    row = await session.execute(
        text("""
            SELECT id, user_id, family_id, expires_at, revoked
            FROM refresh_tokens
            WHERE token_hash = :th
            FOR UPDATE
        """),
        {"th": hash_refresh_token(raw_token)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def revoke_token(session: AsyncSession, token_id: UUID, reason: str) -> None:
    await session.execute(
        text("""
            UPDATE refresh_tokens
            SET revoked = TRUE, revoked_reason = :reason
            WHERE id = :tid AND revoked = FALSE
        """),
        {"tid": str(token_id), "reason": reason},
    )


async def revoke_family(session: AsyncSession, family_id: UUID, reason: str) -> int:
    """撤销整个令牌家族（重放检测命中时调用，§6.3）。返回撤销数量。"""
    row = await session.execute(
        text("""
            WITH revoked AS (
                UPDATE refresh_tokens
                SET revoked = TRUE, revoked_reason = :reason
                WHERE family_id = :fid AND revoked = FALSE
                RETURNING 1
            )
            SELECT count(*) FROM revoked
        """),
        {"fid": str(family_id), "reason": reason},
    )
    return int(row.scalar_one())


# ---------------- audit（§6.8，M1 覆盖认证事件）----------------

async def write_audit(
    session: AsyncSession,
    *,
    user_id: UUID | None,
    actor_type: str,
    action: str,
    target: str | None = None,
    payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> None:
    await session.execute(
        text("""
            INSERT INTO audit_logs (user_id, actor_type, action, target, payload, trace_id)
            VALUES (:uid, :actor, :action, :target, CAST(:payload AS JSONB), :trace)
        """),
        {
            "uid": str(user_id) if user_id else None,
            "actor": actor_type,
            "action": action,
            "target": target,
            "payload": None if payload is None else json.dumps(payload, ensure_ascii=False),
            "trace": trace_id,
        },
    )
