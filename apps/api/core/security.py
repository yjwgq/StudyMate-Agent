"""鉴权原语：密码哈希（bcrypt）、JWT 签发校验、refresh token 生成与哈希。

设计要点（§6.3 / §13.1）：
    - access token：JWT（HS256，30 分钟），无状态、不可撤销 —— 30min 窗口风险可接受；
    - refresh token：不透明随机串（48 字节 urlsafe），服务端只存 sha256 哈希；
      轮转 + family 重放检测在仓储层实现（见 repositories/auth.py）。
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import bcrypt
import jwt

from apps.api.core.config import settings
from apps.api.core.errors import AppError, ErrorCode

_BCRYPT_ROUNDS = 12


# ---------------- 密码 ----------------

def hash_password(password: str) -> str:
    """bcrypt 哈希。72 字节是 bcrypt 的硬限制，超长直接拒绝（不是截断）。"""
    if len(password.encode("utf-8")) > 72:
        raise AppError(ErrorCode.VALIDATION, "密码过长（最多 72 字节）", 422)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode())
    except ValueError:
        # 库中的哈希损坏时按「校验失败」处理，而不是 500
        return False


# ---------------- Access Token（JWT）----------------

def create_access_token(user_id: UUID, ttl_min: int | None = None) -> tuple[str, int]:
    """签发 access token，返回 (token, expires_in_seconds)。"""
    ttl = (settings.access_ttl_min if ttl_min is None else ttl_min) * 60
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        "jti": secrets.token_hex(8),
        "typ": "access",
    }
    token = jwt.encode(payload, _require_secret(), algorithm="HS256")
    return token, ttl


def decode_access_token(token: str) -> UUID:
    """校验并解析 access token；失败按错误码字典细分。"""
    try:
        payload = jwt.decode(token, _require_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise AppError(ErrorCode.TOKEN_EXPIRED, "登录已过期，请重新登录", 401) from exc
    except jwt.InvalidTokenError as exc:
        raise AppError(ErrorCode.AUTH_FAILED, "无效的认证凭据", 401) from exc
    if payload.get("typ") != "access":
        raise AppError(ErrorCode.AUTH_FAILED, "无效的认证凭据", 401)
    try:
        return UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise AppError(ErrorCode.AUTH_FAILED, "无效的认证凭据", 401) from exc


# ---------------- Refresh Token（不透明随机串）----------------

def generate_refresh_token() -> str:
    """48 字节随机数（~384 bit 熵），经 HTTPS 传输、服务端只存哈希。"""
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw: str) -> str:
    """sha256(raw) 的十六进制串 —— 高熵随机令牌用 sha256 存储是标准做法。"""
    return hashlib.sha256(raw.encode()).hexdigest()


def _require_secret() -> str:
    if not settings.jwt_configured:
        raise AppError(
            ErrorCode.INTERNAL,
            "JWT_SECRET 未配置：请在 .env 中填写后重启服务",
            503,
        )
    return settings.jwt_secret
