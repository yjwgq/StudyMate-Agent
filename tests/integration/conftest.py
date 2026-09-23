"""Agent 集成测试共享夹具（tests/integration/，M4-12）。

与 tests/security 同款的环境改写（宿主机地址），但不启 HTTP ——
直接驱动 agent/graph/runner（fake LLM 全图，§16 集成测试层）。
"""

import contextlib
import hashlib
import os
import uuid
from pathlib import Path

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
    return url.replace("@postgres:5432", "@127.0.0.1:15432")


def _to_host_redis(url: str) -> str:
    return url.replace("redis://redis:6379", "redis://127.0.0.1:16379")


os.environ["DATABASE_URL"] = _to_host_db(_DOTENV.get("DATABASE_URL", ""))
os.environ["DATABASE_URL_WORKER"] = _to_host_db(_DOTENV.get("DATABASE_URL_WORKER", ""))
os.environ["REDIS_URL"] = _to_host_redis(_DOTENV.get("REDIS_URL", "redis://redis:6379/0"))


@pytest_asyncio.fixture(scope="session")
async def worker_engine():
    engine = create_async_engine(os.environ["DATABASE_URL_WORKER"], pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def test_user(worker_engine):
    """插入真实用户行（tool_invocations / audit_logs 的 FK 依赖）。"""
    email = f"agent-{uuid.uuid4().hex[:10]}@test.dev"
    # password_hash 非空：放一个与真实认证无关的占位哈希（本测试不走登录）
    fake_hash = hashlib.sha256(email.encode()).hexdigest()
    async with worker_engine.begin() as conn:
        row = await conn.execute(
            text(
                "INSERT INTO users (email, password_hash, display_name) "
                "VALUES (:e, :ph, 'agent-test') RETURNING id"
            ),
            {"e": email, "ph": fake_hash},
        )
        uid = str(row.scalar_one())
    yield uid
    async with worker_engine.begin() as conn:
        await conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})


@pytest_asyncio.fixture
async def registry(worker_engine):
    """真实工具注册表：内置检索/fetch + stub 工具 + MCP（sandbox/todo）。

    redis 传 None：工具层幂等去重关闭（测试断言的是执行与埋点，不是缓存）。
    """
    from agent.tools.builtin.retrieval_tool import build_builtin_tools
    from agent.tools.policy import ToolPolicy, bootstrap_policies, get_policy_registry
    from agent.tools.registry import ToolRegistry
    from tests.integration.scripted_tools import HugeTool, SlowTool

    bootstrap_policies()
    # 测试桩工具的本地策略（§13.10：风险等级本地定）
    get_policy_registry().register("slow_probe", ToolPolicy(risk_level_override=0))
    get_policy_registry().register("huge_probe", ToolPolicy(risk_level_override=0))
    reg = ToolRegistry(redis=None)
    for tool in build_builtin_tools():
        reg.register(tool)
    for tool in (SlowTool(), HugeTool()):
        reg.register(tool)
    try:
        from agent.tools.mcp_client import McpClientPool, McpServerSpec

        pool = McpClientPool([
            McpServerSpec("sandbox", "mcp_servers.sandbox_server", timeout_s=10.0),
            McpServerSpec("todo", "mcp_servers.todo_server", timeout_s=10.0),
            McpServerSpec("email", "mcp_servers.email_server", timeout_s=10.0),
        ])
        for tool in await pool.discover():
            with contextlib.suppress(ValueError):
                reg.register(tool)
    except Exception:
        pass  # MCP 不可用时其余工具照常（显式降级）
    yield reg
