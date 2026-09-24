"""Agent 运行时的领域类型（M4-1，§7.2）。

只用 Pydantic 做数据校验与描述，不承载执行逻辑（执行体在 BaseTool，§7.5）。
这些类型会进入 LangGraph state / checkpoint，字段保持可 JSON 序列化。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Chunk(BaseModel):
    """检索命中的最小单元（与 agent.retrieval.search.Hit 对应的 state 版本）。"""

    chunk_id: str
    parent_chunk_id: str | None = None
    document_id: str
    document_title: str = ""
    score: float = 0.0
    content: str = ""          # 子块摘要
    parent_content: str = ""   # 父块全文（上下文用）
    page: int | None = None


class Citation(BaseModel):
    """引用脚注（§8.4 结构，与 messages.citations JSONB 对齐）。"""

    n: int
    chunk_id: str | None = None
    parent_chunk_id: str | None = None
    document_id: str | None = None
    title: str = ""
    page: int | None = None
    snippet: str = ""
    score: float | None = None
    rerank_score: float | None = None


class Step(BaseModel):
    """DAG 计划的一步（§7.1 / §7.2）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: int
    description: str
    tool_hint: str | None = None
    depends_on: list[int] = Field(default_factory=list)
    status: Literal["pending", "running", "done", "failed", "blocked", "skipped"] = "pending"
    result: str = ""
    attempts: int = 0
    error: str | None = None
    # 执行元数据（M4 新增，供 executor 聚合预算与日志）
    started_at: float | None = None
    finished_at: float | None = None
    tokens_used: int = 0


class Artifact(BaseModel):
    """Agent 产出的报告/文件引用（§7.2）。"""

    kind: Literal["text", "file", "report"] = "text"
    name: str
    ref: str          # full_ref / 文件 id
    summary: str = ""


class PlanIssue(BaseModel):
    """planner 解析失败/降级的原因（trace 用）。"""

    reason: str
    raw: str = ""


def citations_key(c: Any) -> str:
    """merge_unique 的 key：同一来源 chunk 只保留一条引用（§7.2 用 chunk_id 去重）。

    并行 step 的局部编号 n 不参与去重 —— 全局 [n] 由 synthesize 前统一重编号。
    """
    if isinstance(c, Citation):
        return str(c.chunk_id)
    if isinstance(c, dict):
        return str(c.get("chunk_id"))
    return str(c)


def chunks_key(c: Any) -> str:
    if isinstance(c, Chunk):
        return c.chunk_id
    if isinstance(c, dict):
        return str(c.get("chunk_id"))
    return str(c)


TOOL_HINTS = ("retrieval", "search", "todo", "sandbox", "fetch_full")
"""planner 允许给的 tool_hint 白名单（校验用；越界值按普通文本 step 处理）。"""
