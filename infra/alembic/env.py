"""Alembic 迁移环境。

连接串来源（优先级从高到低）：
    1. 环境变量 DATABASE_URL_MIGRATE —— app_owner 迁移角色（标准路径）
    2. 环境变量 DATABASE_URL          —— 兜底

驱动说明：.env 里的 URL 是 asyncpg 格式（postgresql+asyncpg://），
迁移用同步驱动执行更简单可靠 —— 这里统一替换为 psycopg3（postgresql://），
psycopg 由 langgraph-checkpoint-postgres 传递依赖引入，无需额外安装。
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 纯 SQL 迁移（DDL 与设计文档 v1.1 §6 逐条对应），不使用 ORM metadata
target_metadata = None


def _resolve_url() -> str:
    raw = os.environ.get("DATABASE_URL_MIGRATE") or os.environ.get("DATABASE_URL") or ""
    if not raw:
        raise RuntimeError(
            "缺少数据库连接串：请设置 DATABASE_URL_MIGRATE（app_owner 迁移角色）。"
            "开发环境下它来自项目根目录的 .env。"
        )
    # asyncpg URL -> psycopg3 URL（同步驱动）
    return raw.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 不连库（alembic upgrade head --sql）。"""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：直连数据库执行。"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
