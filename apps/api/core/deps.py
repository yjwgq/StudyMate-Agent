"""FastAPI 依赖：从请求头解析并注入当前用户。

所有需要租户上下文的端点都以 `user: UserCtx = Depends(current_user)` 开头；
解析失败的错误码细分（401 过期 / 401 无效）由 security.decode_access_token 抛出。
"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request

from apps.api.core.db import session_scope
from apps.api.core.errors import AppError, ErrorCode
from apps.api.core.security import decode_access_token
from apps.api.repositories import auth as auth_repo


@dataclass(frozen=True, slots=True)
class UserCtx:
    id: UUID
    email: str
    role: str


def _extract_bearer(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AppError(ErrorCode.AUTH_FAILED, "缺少 Bearer 凭据", 401)
    return token.strip()


async def current_user(request: Request) -> UserCtx:
    """解析 JWT → 查库确认用户存在且启用。"""
    user_id = decode_access_token(_extract_bearer(request))
    async with session_scope() as session:
        row = await auth_repo.get_user_by_id(session, user_id)
    if row is None or not row["is_active"]:
        raise AppError(ErrorCode.AUTH_FAILED, "用户不存在或已停用", 401)
    return UserCtx(id=row["id"], email=row["email"], role=row["role"])


def require_admin(user: Annotated[UserCtx, Depends(current_user)]) -> UserCtx:
    """admin 路由的角色闸门（§13.1）；M1 先立闸门，/admin/metrics 在 M9 落地。"""
    if user.role != "admin":
        raise AppError(ErrorCode.FORBIDDEN, "需要管理员权限", 403)
    return user
