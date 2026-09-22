"""安全集成测试共享夹具（tests/security/）。

运行前提（B8 验收）：
    1. 开发栈已启动：docker compose -f infra/docker-compose.dev.yml up -d
       （postgres 映射 127.0.0.1:15432、redis 映射 127.0.0.1:16379）
    2. .env 已完成 M1 准备（JWT_SECRET + 三条 DATABASE_URL 密码）

设计说明：
    - 测试用 httpx.ASGITransport 在进程内直接驱动 FastAPI 应用（不占端口）；
    - conftest 在导入 apps.* 之前把 DATABASE_URL 等改写为宿主机地址——
      pydantic-settings 的优先级是 环境变量 > .env，进程内应用与测试
      读到的是同一套连接串；
    - 「为用户 B 预置会话」等跨租户数据操作走 app_worker 连接（BYPASSRLS，
      模拟 worker 的合法跨租户写入）；RLS 探测走 app_api 连接。
"""

import os
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> dict[str, str]:
    env: dict[str, str] = {}
    p = PROJECT_ROOT / ".env"
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


_DOTENV = _load_dotenv()


def _to_host_db(url: str) -> str:
    """容器内地址 → 宿主机映射地址（postgres:5432 → 127.0.0.1:15432）。"""
    return url.replace("@postgres:5432", "@127.0.0.1:15432")


def _to_host_redis(url: str) -> str:
    return url.replace("redis://redis:6379", "redis://127.0.0.1:16379")


# ---- 在导入 apps.* 之前固定环境（pydantic-settings：环境变量 > .env）----
os.environ["DATABASE_URL"] = _to_host_db(_DOTENV.get("DATABASE_URL", ""))
os.environ["DATABASE_URL_WORKER"] = _to_host_db(_DOTENV.get("DATABASE_URL_WORKER", ""))
os.environ["DATABASE_URL_MIGRATE"] = _to_host_db(_DOTENV.get("DATABASE_URL_MIGRATE", ""))
os.environ["REDIS_URL"] = _to_host_redis(
    _DOTENV.get("REDIS_URL", "redis://redis:6379/0")
)
if not _DOTENV.get("JWT_SECRET") and not os.environ.get("JWT_SECRET"):
    raise RuntimeError(
        "JWT_SECRET 未配置：请先完成 .env 的 M1 准备（openssl rand -hex 32）"
    )


@pytest.fixture(scope="session")
def app():
    from apps.api.main import app as fastapi_app

    return fastapi_app


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------- 数据库夹具（跨租户预置 / RLS 探测）----------------

@pytest_asyncio.fixture(scope="session")
async def worker_engine():
    """app_worker（BYPASSRLS）：模拟 worker 的合法跨租户数据操作。"""
    engine = create_async_engine(os.environ["DATABASE_URL_WORKER"], pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def api_engine():
    """app_api（受 RLS 约束）：用于探测「没有租户上下文时是否默认拒绝」。"""
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    yield engine
    await engine.dispose()


async def insert_conversation_for(worker_engine, user_id: str) -> str:
    """以 worker 身份为指定用户预置一个会话（绕过 RLS，模拟合法后台写入）。"""
    async with worker_engine.begin() as conn:
        row = await conn.execute(
            text("""
                INSERT INTO conversations (user_id, title)
                VALUES (:uid, :title)
                RETURNING id
            """),
            {"uid": user_id, "title": "fixture-conversation"},
        )
        return str(row.scalar_one())


# ---------------- API 辅助 ----------------

TEST_PASSWORD = "S3cure-Passw0rd!"


async def register_and_login(client: httpx.AsyncClient, email: str | None = None) -> dict:
    """注册 + 登录，返回 {email, access_token, refresh_token, user_id}。"""
    email = email or f"t-{uuid.uuid4().hex[:12]}@test.dev"
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": TEST_PASSWORD, "display_name": "Tester"},
    )
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": TEST_PASSWORD},
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()["data"]
    return {
        "email": email,
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "user_id": data["user"]["id"],
    }


def auth_header(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}
