"""认证接口（M1-4）：注册 / 登录 / 刷新 / 登出。

契约（§11.2）：POST /api/v1/auth/{register,login,refresh,logout}
- access 30min（JWT）+ refresh 7d（不透明串，库中只存哈希）
- refresh 轮转：每次刷新旧 token 作废、签发新 token（同 family）
- 重放检测：已撤销 token 再次使用 → 撤销整个 family + 401 TOKEN_REUSE_DETECTED（B5）
"""

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field

from apps.api.core.config import settings
from apps.api.core.db import session_scope
from apps.api.core.deps import UserCtx, current_user
from apps.api.core.errors import AppError, ErrorCode
from apps.api.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from apps.api.repositories import auth as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    display_name: str | None = Field(default=None, max_length=64)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=256)


class LogoutIn(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=256)


def _token_pair_payload(
    *,
    user_id,
    access_token: str,
    expires_in: int,
    refresh_token: str,
    email: str,
    role: str,
) -> dict:
    return {
        "access_token": access_token,
        "expires_in": expires_in,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {"id": str(user_id), "email": email, "role": role},
    }


@router.post("/register", status_code=201)
async def register(body: RegisterIn, request: Request) -> dict:
    """注册。成功后需另行登录（B3 验收流程：注册 → 登录 → 调用业务接口）。"""
    trace_id = getattr(request.state, "trace_id", "")
    async with session_scope() as session:
        existing = await repo.get_user_by_email(session, body.email)
        if existing is not None:
            raise AppError(
                ErrorCode.VALIDATION, "该邮箱已被注册", 409,
                detail={"field": "email"},
            )
        user = await repo.create_user(
            session,
            email=body.email,
            password_hash=hash_password(body.password),
            display_name=body.display_name,
        )
        await repo.write_audit(
            session, user_id=user["id"], actor_type="user",
            action="auth.register", trace_id=trace_id,
        )
    return {
        "data": {"user": {
            "id": str(user["id"]),
            "email": user["email"],
            "display_name": user["display_name"],
        }},
        "trace_id": trace_id,
    }


@router.post("/login")
async def login(body: LoginIn, request: Request) -> dict:
    """登录：验证密码 → 签发 access + refresh（新 family）。"""
    trace_id = getattr(request.state, "trace_id", "")
    async with session_scope() as session:
        user = await repo.get_user_by_email(session, body.email)
        # 统一错误信息：不向未认证者暴露「邮箱是否存在」
        if user is None or not verify_password(body.password, user["password_hash"]):
            await repo.write_audit(
                session, user_id=None, actor_type="user",
                action="auth.login_failed", target=body.email, trace_id=trace_id,
            )
            raise AppError(ErrorCode.AUTH_FAILED, "邮箱或密码错误", 401)
        if not user["is_active"]:
            raise AppError(ErrorCode.AUTH_FAILED, "账号已停用", 401)

        family = await repo.new_family()
        refresh_raw, _ = await repo.issue_refresh_token(
            session, user["id"], family, settings.refresh_ttl_days
        )
        await repo.touch_last_login(session, user["id"])
        await repo.write_audit(
            session, user_id=user["id"], actor_type="user",
            action="auth.login", trace_id=trace_id,
        )
        access, expires_in = create_access_token(user["id"])
    return {
        "data": _token_pair_payload(
            user_id=user["id"], access_token=access, expires_in=expires_in,
            refresh_token=refresh_raw, email=user["email"], role=user["role"],
        ),
        "trace_id": trace_id,
    }


@router.post("/refresh")
async def refresh(body: RefreshIn, request: Request) -> dict:
    """刷新：轮转 + 重放检测（§6.3）。

    流程（FOR UPDATE 行锁把并发刷新串行化）：
        1. 已撤销 → 凭据泄露 → 撤销整个 family + 401 TOKEN_REUSE_DETECTED（B5）；
        2. 已过期 → 401 TOKEN_EXPIRED；
        3. 正常 → 旧 token 作废（rotated），同 family 签发新对。

    重放分支必须**先随事务提交撤销、出块后再抛 401**：若在事务块内直接
    raise，session.begin() 的异常回滚会把 revoke_family 与审计一并回滚，
    家族连坐静默失效（B5-3 用例抓到的真实 bug）。
    """
    trace_id = getattr(request.state, "trace_id", "")
    reuse_detected = False
    async with session_scope() as session:
        token = await repo.lock_token_by_hash(session, body.refresh_token)
        if token is None:
            raise AppError(ErrorCode.AUTH_FAILED, "无效的刷新凭据", 401)

        if token["revoked"]:
            # 重放：已撤销的 token 再次使用 = 凭据泄露（§6.3）
            revoked_n = await repo.revoke_family(
                session, token["family_id"], "reuse_detected"
            )
            await repo.write_audit(
                session, user_id=token["user_id"], actor_type="user",
                action="auth.refresh_reuse_detected",
                payload={"revoked_tokens": revoked_n}, trace_id=trace_id,
            )
            logger.warning(
                "refresh reuse detected user=%s family=%s revoked=%s",
                token["user_id"], token["family_id"], revoked_n,
            )
            reuse_detected = True
        elif token["expires_at"].timestamp() < time.time():
            raise AppError(ErrorCode.TOKEN_EXPIRED, "刷新凭据已过期，请重新登录", 401)
        else:
            # 轮转：旧 token 作废，同 family 签发新对
            await repo.revoke_token(session, token["id"], "rotated")
            refresh_raw, _ = await repo.issue_refresh_token(
                session, token["user_id"], token["family_id"], settings.refresh_ttl_days
            )
            access, expires_in = create_access_token(token["user_id"])

            user = await repo.get_user_by_id(session, token["user_id"])
            if user is None or not user["is_active"]:
                raise AppError(ErrorCode.AUTH_FAILED, "用户不存在或已停用", 401)
            # 出事务块前解析成标量：mypy 无法跨块保留 user 非 None 的收窄
            uemail, urole = user["email"], user["role"]
            uid = str(token["user_id"])

    if reuse_detected:
        raise AppError(
            ErrorCode.TOKEN_REUSE_DETECTED,
            "检测到令牌重放，相关会话已全部失效，请重新登录",
            401,
        )
    return {
        "data": _token_pair_payload(
            user_id=uid, access_token=access, expires_in=expires_in,
            refresh_token=refresh_raw, email=uemail, role=urole,
        ),
        "trace_id": trace_id,
    }


@router.post("/logout")
async def logout(
    body: LogoutIn,
    request: Request,
    user: Annotated[UserCtx, Depends(current_user)],
) -> dict:
    """登出：撤销给定 refresh token。

    幂等：token 不存在 / 已撤销都按成功处理。
    安全细节：只允许撤销**本人**的 token（哈希查到但 user_id 不匹配时静默忽略，
    不向调用方泄露该 token 属于谁）。
    """
    trace_id = getattr(request.state, "trace_id", "")
    async with session_scope() as session:
        token = await repo.lock_token_by_hash(session, body.refresh_token)
        if token is not None and str(token["user_id"]) == str(user.id):
            await repo.revoke_token(session, token["id"], "logout")
            await repo.write_audit(
                session, user_id=user.id, actor_type="user",
                action="auth.logout", trace_id=trace_id,
            )
    return {"data": {"ok": True}, "trace_id": trace_id}
