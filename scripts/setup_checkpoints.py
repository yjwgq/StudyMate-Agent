"""M1-3：创建 LangGraph checkpoint 三表（§6.9）。

由 langgraph-checkpoint-postgres 库自带的迁移脚本生成
    checkpoints / checkpoint_writes / checkpoint_blobs
语句均为幂等（IF NOT EXISTS / CREATE INDEX IF NOT EXISTS），重复运行安全。

用法（在 migrate 容器内由 compose 自动执行）：
    uv run --no-dev python scripts/setup_checkpoints.py
"""

import os
import sys

# 依赖的导入失败要给出可读信息，而不是一屏堆栈
try:
    from langgraph.checkpoint.postgres import PostgresSaver
except ImportError as exc:  # pragma: no cover
    sys.exit(f"缺少依赖 langgraph-checkpoint-postgres（pyproject.toml 中添加）：{exc}")


def main() -> None:
    raw = os.environ.get("DATABASE_URL_MIGRATE") or os.environ.get("DATABASE_URL") or ""
    if not raw:
        sys.exit("缺少 DATABASE_URL_MIGRATE —— 请检查 .env（app_owner 迁移角色的连接串）")
    # .env 中的 URL 是 asyncpg 格式；这里用同步 psycopg 驱动
    dsn = raw.replace("postgresql+asyncpg://", "postgresql://")

    # from_conn_string 返回上下文管理器；setup() 幂等
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()
    print("[checkpoints] checkpoints / checkpoint_writes / checkpoint_blobs ready")


if __name__ == "__main__":
    main()
