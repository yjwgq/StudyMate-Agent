"""待办 MCP server（M4-9，ADR-4）。

todo CRUD 走数据库直连（asyncpg，worker DSN / BYPASSRLS）：
    - server 是可信基础设施进程；
    - user_id / dedupe_key 由**治理层注入**（registry injected_params），
      LLM 的 schema 里看不到这两个参数，无法伪造租户身份；
    - UNIQUE(user_id, dedupe_key) 在 DB 层兜底幂等（§7.6 存储层）。
"""

import json
import os
from datetime import UTC, datetime

import asyncpg
from pydantic import BaseModel, Field

from mcp_servers.common import make_server, run

app = make_server(
    "todo",
    "管理用户的待办事项：创建、查询、完成。创建待办属于写操作。",
)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.environ.get("DATABASE_URL_WORKER", "").replace("postgresql+asyncpg://", "postgresql://")
        if not dsn:
            raise RuntimeError("DATABASE_URL_WORKER 未配置")
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, command_timeout=10)
    return _pool


class CreateTodoArgs(BaseModel):
    title: str = Field(min_length=1, max_length=200, description="待办标题")
    detail: str | None = Field(default=None, max_length=2000, description="补充说明")
    due_at: str | None = Field(default=None, max_length=40, description="截止时间（ISO 8601，可空）")
    # user_id / dedupe_key：治理层注入参数（不出现在 LLM schema）
    user_id: str = ""
    dedupe_key: str | None = None


class ListTodosArgs(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)
    user_id: str = ""  # 注入参数


class CompleteTodoArgs(BaseModel):
    todo_id: str = Field(min_length=1, max_length=64, description="待办 id")
    user_id: str = ""  # 注入参数


def _parse_due(due_at: str | None) -> datetime | None:
    if not due_at:
        return None
    try:
        dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


@app.tool()
async def create_todo(
    title: str, detail: str | None = None, due_at: str | None = None,
    user_id: str = "", dedupe_key: str | None = None,
) -> str:
    """创建待办事项（写操作，事后告知用户）。"""
    pool = await get_pool()
    due = _parse_due(due_at)
    async with pool.acquire() as conn:
        if dedupe_key:
            row = await conn.fetchrow(
                """
                INSERT INTO todos (user_id, title, detail, due_at, dedupe_key, source)
                VALUES ($1, $2, $3, $4, $5, 'chat')
                ON CONFLICT (user_id, dedupe_key) DO UPDATE SET updated_at = now()
                RETURNING id, title,
                          (xmax = 0) AS inserted
                """,
                user_id, title, detail, due, dedupe_key,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO todos (user_id, title, detail, due_at, source)
                VALUES ($1, $2, $3, $4, 'chat')
                RETURNING id, title, TRUE AS inserted
                """,
                user_id, title, detail, due,
            )
    return json.dumps(
        {
            "ok": True,
            "todo_id": str(row["id"]),
            "title": row["title"],
            "created": bool(row["inserted"]),
            "dedupe_key": dedupe_key,
            "note": "已创建待办" if row["inserted"] else "相同待办已存在（幂等命中）",
        },
        ensure_ascii=False,
    )


@app.tool()
async def list_todos(limit: int = 10, user_id: str = "") -> str:
    """列出用户最近的待办。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, detail, due_at, status, created_at
            FROM todos WHERE user_id = $1
            ORDER BY created_at DESC LIMIT $2
            """,
            user_id, limit,
        )
    return json.dumps(
        {
            "ok": True,
            "todos": [
                {
                    "id": str(r["id"]),
                    "title": r["title"],
                    "detail": r["detail"],
                    "due_at": r["due_at"].isoformat() if r["due_at"] else None,
                    "status": r["status"],
                }
                for r in rows
            ],
        },
        ensure_ascii=False,
    )


@app.tool()
async def complete_todo(todo_id: str, user_id: str = "") -> str:
    """把待办标记为完成。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE todos SET status = 'done', updated_at = now()
            WHERE id = $1 AND user_id = $2
            RETURNING id, title
            """,
            todo_id, user_id,
        )
    if row is None:
        return json.dumps({"ok": False, "error": "待办不存在"}, ensure_ascii=False)
    return json.dumps({"ok": True, "todo_id": str(row["id"]), "title": row["title"]}, ensure_ascii=False)


if __name__ == "__main__":
    # 进程随 stdio 会话结束而退出，asyncpg 池由进程回收，无需显式 close
    run(app)
