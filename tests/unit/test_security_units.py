"""鉴权原语单测（纯函数，毫秒级，无外部依赖）。

覆盖：bcrypt 哈希往返、JWT 签发/过期/伪造、refresh token 熵与哈希稳定性。
集成级安全用例在 tests/security/。
"""


from uuid import uuid4

import pytest

from apps.api.core.errors import AppError, ErrorCode
from apps.api.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)


def test_password_hash_roundtrip():
    h = hash_password("S3cure-Passw0rd!")
    assert h != "S3cure-Passw0rd!"
    assert h.startswith("$2b$")  # bcrypt 标识
    assert verify_password("S3cure-Passw0rd!", h)
    assert not verify_password("wrong-password", h)


def test_password_rejects_over_72_bytes():
    with pytest.raises(AppError) as exc:
        hash_password("x" * 73)
    assert exc.value.code == ErrorCode.VALIDATION


def test_jwt_roundtrip():
    uid = uuid4()
    token, ttl = create_access_token(uid, ttl_min=5)
    assert ttl == 300
    assert decode_access_token(token) == uid


def test_jwt_expired_rejected():
    uid = uuid4()
    token, _ = create_access_token(uid, ttl_min=-1)  # 已过期
    with pytest.raises(AppError) as exc:
        decode_access_token(token)
    assert exc.value.code == ErrorCode.TOKEN_EXPIRED


def test_jwt_forged_signature_rejected():
    uid = uuid4()
    token, _ = create_access_token(uid)
    tampered = token[:-6] + ("aaaaaa" if not token.endswith("aaaaaa") else "bbbbbb")
    with pytest.raises(AppError) as exc:
        decode_access_token(tampered)
    assert exc.value.code == ErrorCode.AUTH_FAILED


def test_refresh_token_entropy_and_hash_stability():
    raw1 = generate_refresh_token()
    raw2 = generate_refresh_token()
    assert raw1 != raw2                       # 不可预测
    assert len(raw1) >= 48                    # 高熵
    assert hash_refresh_token(raw1) == hash_refresh_token(raw1)  # 稳定
    assert hash_refresh_token(raw1) != hash_refresh_token(raw2)
    assert len(hash_refresh_token(raw1)) == 64  # sha256 hex


def test_jwt_secret_missing_fails_loudly(monkeypatch):
    from apps.api.core import security
    from apps.api.core.config import settings

    monkeypatch.setattr(security, "_require_secret", lambda: (_ for _ in ()).throw(
        AppError(ErrorCode.INTERNAL, "JWT_SECRET 未配置", 503)
    ))
    with pytest.raises(AppError):
        create_access_token(uuid4())
    _ = settings  # 显式引用，说明本用例关注配置缺失行为
