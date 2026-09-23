"""数据库会话管理（asyncpg + SQLAlchemy 2.x async）。

租户上下文（ADR-9，B4 验收的核心机制）：
    RLS 策略读 `current_setting('app.user_id', true)`。
    - 必须在**事务内**设置 —— asyncpg 是连接池复用，会话级 SET 会泄漏到
      下一个请求（隐蔽且严重的问题）；
    - 用 `SELECT set_config('app.user_id', :uid, true)`，第三参 true 等价于
      SET LOCAL（事务结束自动还原），且支持参数绑定（SET LOCAL 本身不支持 $1）；
    - 未设置时 current_setting 返回 NULL，RLS 比较为空 → 默认拒绝（fail-closed）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from logging import getLogger
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apps.api.core.config import settings

logger = getLogger(__name__)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """惰性创建引擎（模块导入期不连库，应用无库也能启动，由 /ready 报告问题）。"""
    if not settings.db_configured:
        raise RuntimeError(
            "DATABASE_URL 未配置：请在 .env 中填写数据库连接串（app_api 角色）"
        )
    engine = create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,   # 连接池复用前探活，避免拿到被服务端断开的连接
        pool_recycle=1800,
    )
    event.listens_for(engine.sync_engine, "connect")(_on_connect)
    return engine


def _on_connect(dbapi_conn: Any, _record: Any) -> None:
    """每个新连接执行一次的连接级设置。

    hnsw.iterative_scan（pgvector ≥ 0.8，M3/M6 依赖）：带 user_id 过滤时，
    普通 HNSW「先取 top-k 再过滤」会丢结果，iterative_scan 在图遍历中补页。
    会话级设置只影响 HNSW 过滤扫描；GUC 不存在（旧版 pgvector）时静默跳过，
    检索退化为普通 HNSW 行为 —— 连接层 try/except 避免毒化事务。
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("SET hnsw.iterative_scan = 'iterative_scan'")
    except Exception:  # noqa: BLE001 —— 旧版 pgvector 无该 GUC，降级可用
        logger.warning("pgvector iterative_scan GUC 不可用，检索未启用补页扫描")
    finally:
        cur.close()


@lru_cache(maxsize=1)
def _get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """无租户上下文的事务会话：用于注册 / 登录 / 刷新等「身份确立之前」的流程。"""
    session = _get_sessionmaker()()
    try:
        async with session.begin():
            yield session
    except Exception:
        # session.begin() 上下文退出时自动 rollback，这里显式抛出保持语义清晰
        raise
    finally:
        await session.close()


@asynccontextmanager
async def tenant_session(user_id: UUID) -> AsyncIterator[AsyncSession]:
    """带租户上下文的事务会话：事务内 SET LOCAL app.user_id（ADR-9 第 1 条）。

    所有触碰 RLS 业务表的代码都必须经由它；事务结束自动还原，
    连接池复用不会把租户 ID 带到下一个请求。
    """
    session = _get_sessionmaker()()
    try:
        async with session.begin():
            # set_config(..., is_local := true) ≡ SET LOCAL，且支持参数绑定
            await session.execute(
                text("SELECT set_config('app.user_id', :uid, true)"),
                {"uid": str(user_id)},
            )
            yield session
    finally:
        await session.close()


async def check_db() -> str | None:
    """就绪探针用：连通返回 None，失败返回可读原因。"""
    if not settings.db_configured:
        return "DATABASE_URL 未配置"
    try:
        session = _get_sessionmaker()()
        try:
            await session.execute(text("SELECT 1"))
        finally:
            await session.close()
    except Exception as exc:  # noqa: BLE001 —— 探针要把原因带出去
        return f"数据库连接失败：{type(exc).__name__}: {exc}"
    return None
