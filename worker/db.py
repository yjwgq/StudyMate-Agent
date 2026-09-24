"""Worker 侧同步数据库会话。

Celery prefork worker 是同步进程，不能复用 API 的 asyncpg 异步引擎。
统一改用 psycopg3 同步驱动：连接串把 `+asyncpg` 换成 `+psycopg`
（psycopg[binary] 已在依赖里，迁移与 checkpoint 脚本同款）。

worker 连的是 DATABASE_URL_WORKER（app_worker 角色，BYPASSRLS，ADR-9）：
ingest 是「后台合法跨租户写入」，但仍显式按 document 自带的 user_id
条件写 chunks，保持与仓储层同一的双保险习惯。
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from apps.api.core.config import settings


def to_sync_dsn(url: str) -> str:
    """postgresql+asyncpg:// → postgresql+psycopg://（worker 同步栈）。"""
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql+psycopg://"):
        return url
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


_engine = None
_SessionLocal: sessionmaker | None = None


def get_worker_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            to_sync_dsn(settings.database_url_worker),
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=2,
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """同步事务会话：正常提交，异常回滚（与 API 侧 session_scope 语义一致）。"""
    get_worker_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
