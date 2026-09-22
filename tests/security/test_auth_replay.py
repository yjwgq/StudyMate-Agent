"""B5：refresh token 轮转与重放检测。

验收：用同一个 refresh token 刷新两次 →
    第一次成功（轮转：旧作废、新签发，同 family）；
    第二次失败（401 TOKEN_REUSE_DETECTED），且该 family 的全部 token 被撤销。
重放后重新登录不受影响（只撤销被泄露的链路，不是封号）。
"""

import uuid

import pytest

from tests.security.conftest import register_and_login


@pytest.mark.asyncio
async def test_b5_refresh_replay_revokes_family(client):
    tokens = await register_and_login(client)
    old_refresh = tokens["refresh_token"]

    # 第一次刷新：成功，返回新的 token 对
    r1 = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": old_refresh}
    )
    assert r1.status_code == 200, r1.text
    data1 = r1.json()["data"]
    assert data1["access_token"]
    assert data1["refresh_token"] != old_refresh, "轮转后必须签发新 refresh token"

    # 第二次用同一个旧 token：重放 → 401 TOKEN_REUSE_DETECTED
    r2 = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": old_refresh}
    )
    assert r2.status_code == 401, f"期望 401，实际 {r2.status_code}"
    assert r2.json()["error"]["code"] == "TOKEN_REUSE_DETECTED"

    # 重放检测后：轮转得到的新 token 也应被连带撤销（整个 family 失效）
    r3 = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": data1["refresh_token"]}
    )
    assert r3.status_code == 401, "family 未被整体撤销"

    # 重新登录不受影响（撤的是令牌链，不是账号）
    r4 = await client.post(
        "/api/v1/auth/login",
        json={"email": tokens["email"], "password": "S3cure-Passw0rd!"},
    )
    assert r4.status_code == 200, r4.text


@pytest.mark.asyncio
async def test_refresh_with_garbage_token_fails_cleanly(client):
    """无效格式的 refresh token → 401 AUTH_FAILED（不 500、不泄露细节）。"""
    r = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": f"garbage-{uuid.uuid4().hex}"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_FAILED"


@pytest.mark.asyncio
async def test_access_token_rejects_forged_signature(client):
    """伪造签名的 access token → 401（JWT 校验生效）。"""
    tokens = await register_and_login(client)
    import base64
    import json as jsonlib

    # 构造一个签名错误的同结构 JWT
    header = base64.urlsafe_b64encode(jsonlib.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        jsonlib.dumps({"sub": tokens["user_id"], "typ": "access", "exp": 9999999999}).encode()
    ).decode().rstrip("=")
    forged = f"{header}.{payload}.invalid-signature"

    r = await client.get(
        "/api/v1/conversations", headers={"Authorization": f"Bearer {forged}"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_FAILED"


@pytest.mark.asyncio
async def test_logout_revokes_refresh_token(client):
    """登出后 refresh token 失效；重复登出幂等（第二次也成功）。"""
    tokens = await register_and_login(client)
    header = {"Authorization": f"Bearer {tokens['access_token']}"}

    r1 = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers=header,
    )
    assert r1.status_code == 200

    # 登出后的 refresh → 已撤销 → 401。
    # 语义：任何已撤销 token 的再次使用一律按「凭据泄露」处理（§6.3），
    # 返回 TOKEN_REUSE_DETECTED 并撤销整个 family —— 与 B5 重放语义一致。
    r2 = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "TOKEN_REUSE_DETECTED"

    # 重复登出（token 已撤销）→ 仍返回成功（幂等）
    r3 = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers=header,
    )
    assert r3.status_code == 200
