"""图组装与运行入口（M4-10，§7.1 / §7.3）。

builder：assemble_graph() —— StateGraph 组装，checkpointer 可选。
runner ：run_agent_turn() —— 面向 chat 端点的高层入口，事件桥接。

Checkpointer（§7.3）：
    PostgresSaver（异步版），thread_id = conversation_id —— 会话状态
    串行独占（Redis 会话锁已保证 §7.4），跨轮消息经 add_messages 累积。
    用 worker DSN（BYPASSRLS）：checkpoint 是基础设施内部状态，
    不是租户业务数据，不应受 RLS 约束。

事件流（runner 桥接 custom stream）：
    graph.astream(..., stream_mode=["custom", "values"])
      custom → (event_name, data) 直接交给回调（chat.py 转 SSE）
      values → 取最终 state（answer/citations/degraded/usage 落库用）
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from agent.graph import nodes
from agent.graph.state import AgentState

logger = logging.getLogger(__name__)

_graph_cache: dict[str, Any] = {}


def assemble_graph(*, checkpointer: BaseCheckpointSaver | None = None) -> Any:
    """组装 Agent 图（无副作用；同参数复用编译结果）。"""

    async def planner(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        llm = config["configurable"]["llm"]
        return await nodes.planner_node(state, llm=llm)

    async def chat(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        cfg = config["configurable"]
        return await nodes.chat_node(
            state, llm=cfg["llm"], groundedness_enabled=cfg.get("groundedness_enabled", True),
            redis=cfg.get("redis"),
        )

    async def execute(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        cfg = config["configurable"]
        return await nodes.execute_node(
            state, llm=cfg["llm"], registry=cfg["registry"], ctx=cfg["tool_ctx"],
            token_budget=cfg.get("token_budget", 30000),
        )

    async def synthesize(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        llm = config["configurable"]["llm"]
        query = nodes._last_user_text(state["messages"])
        return await nodes.synthesize_node(state, llm=llm, user_query=query)

    def route(state: AgentState) -> str:
        return nodes.route_after_planner(state)

    g = StateGraph(AgentState)
    g.add_node("planner_node", planner)
    g.add_node("chat_node", chat)
    g.add_node("execute_node", execute)
    g.add_node("synthesize_node", synthesize)

    g.add_edge(START, "planner_node")
    g.add_conditional_edges(
        "planner_node", route,
        {"chat_node": "chat_node", "execute_node": "execute_node"},
    )
    # chat 分支直接产出答案；plan 分支执行 DAG 后 synthesize
    g.add_edge("chat_node", END)
    g.add_edge("execute_node", "synthesize_node")
    g.add_edge("synthesize_node", END)

    return g.compile(checkpointer=checkpointer)


# ---------------- checkpointer ----------------

_saver: Any = None
_saver_pool: Any = None
_saver_ready = False


async def get_checkpointer() -> BaseCheckpointSaver | None:
    """惰性创建 AsyncPostgresSaver（**连接池**）；未配置/失败返回 None（图照常运行）。

    为什么必须用连接池（M5 实锤）：`from_conn_string` 只开**一条** psycopg
    连接，不同会话的轮次并发跑图时会互相踩踏 → `OperationalError: the
    connection is closed` → 图静默失败（turn 显示 completed 但答案为空）。
    池化后每个操作独立取连接，并发安全。
    """
    global _saver, _saver_pool, _saver_ready
    if _saver_ready:
        return _saver
    _saver_ready = True
    try:
        from apps.api.core.config import settings

        dsn = settings.database_url_worker or settings.database_url
        if not dsn:
            return None
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        # asyncpg URL → psycopg URL
        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        pool = AsyncConnectionPool(
            conninfo=dsn, min_size=1, max_size=8, open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        await pool.open(wait=True, timeout=10)
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        try:
            await saver.setup()  # 幂等；建表由 migrate 负责，此处仅供本地开发
        except Exception as exc:  # noqa: BLE001
            logger.info("checkpoint setup 跳过（表已由迁移创建）：%s", exc)
        _saver = saver
        _saver_pool = pool
        logger.info("checkpointer ready (AsyncPostgresSaver + pool)")
    except Exception:  # noqa: BLE001 —— 无 checkpoint 图仍可运行（降级）
        logger.warning("checkpointer 初始化失败，图将无持久化运行", exc_info=True)
        _saver = None
    return _saver


async def close_checkpointer() -> None:
    global _saver, _saver_pool, _saver_ready
    if _saver_pool is not None:
        try:
            await _saver_pool.close()
        except Exception:  # noqa: BLE001
            logger.debug("checkpointer 池释放失败（忽略）", exc_info=True)
    _saver = None
    _saver_pool = None
    _saver_ready = False


def build_graph() -> Any:
    """进程内唯一图实例（checkpointer 惰性：首次调用前可能还没就绪 → 不带）。"""
    key = "graph"
    if key not in _graph_cache:
        _graph_cache[key] = assemble_graph()
    return _graph_cache[key]


# ---------------- runner ----------------


async def run_agent_turn(
    *,
    llm: Any,
    registry: Any,
    tool_ctx: Any,
    user_id: str,
    conversation_id: str,
    message_id: str = "",
    trace_id: str = "",
    content: str = "",
    redis: Any = None,
    token_budget: int = 30000,
    max_parallel: int = 3,
    groundedness_enabled: bool = True,
    checkpointer: BaseCheckpointSaver | None = None,
    resume_command: Any = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """执行一轮 Agent（或恢复被审批暂停的图），产出事件流。

    产出两类项：
        ("event", {type: citations|token|notice|degraded, ...})  —— 桥接 SSE
        ("final", 最终 state 摘要 dict)                          —— 流结束时的最后一个

    resume_command 非空时忽略 content：输入是 Command(resume=决策)，
    从 thread 的 checkpoint 续跑（M5-2，F3 的「从中断处继续」）。
    """
    graph = assemble_graph(checkpointer=checkpointer) if checkpointer is not None else build_graph()
    if resume_command is not None:
        graph_input: Any = resume_command
    else:
        graph_input = {
            "messages": [{"role": "user", "content": content}],
            "user_id": user_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "trace_id": trace_id,
            "mode": "auto",
            "max_parallel": max_parallel,
            "token_budget": token_budget,
            "degraded": [],
        }
    config = {
        "configurable": {
            "thread_id": conversation_id,
            "llm": llm,
            "registry": registry,
            "tool_ctx": tool_ctx,
            "token_budget": token_budget,
            "groundedness_enabled": groundedness_enabled,
            "redis": redis,  # 供 chat 分支读 feature flag / 工具层复用
        }
    }

    final_state: dict[str, Any] = {}
    try:
        async for mode, chunk in graph.astream(
            graph_input, config, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                event = chunk if isinstance(chunk, dict) else None
                if event and isinstance(event.get("type"), str):
                    yield "event", event
            else:
                final_state = chunk or {}
        yield "final", _summarize(final_state)
    except Exception as exc:  # noqa: BLE001 —— 图级失败转错误事件
        logger.exception("agent graph 执行失败")
        yield "event", {"type": "error", "code": "INTERNAL", "message": str(exc)}
        yield "final", {"error": str(exc)}


def _summarize(state: dict[str, Any]) -> dict[str, Any]:
    """最终 state 的落库摘要（runner 消费）。"""
    if not state:
        return {}
    citations = state.get("citations") or []
    return {
        "answer": state.get("answer") or "",
        "citations": [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in citations],
        "retrieved_count": len(state.get("retrieved") or []),
        "groundedness": state.get("groundedness"),
        "degraded": state.get("degraded") or [],
        "tokens_used": state.get("tokens_used") or 0,
        "mode": state.get("mode") or "chat",
        "plan": [
            s.model_dump() if hasattr(s, "model_dump") else dict(s)
            for s in (state.get("plan") or [])
        ],
        "error": state.get("error"),
        # M5-2：图因 L2 审批暂停时，提取 interrupt 载荷（approval_id 等）
        "awaiting_approval": _extract_interrupt(state),
    }


def _extract_interrupt(state: dict[str, Any]) -> dict[str, Any] | None:
    """从 state 的 __interrupt__ 通道提取审批请求信息。"""
    raw = state.get("__interrupt__")
    if not raw:
        return None
    try:
        items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        for it in items:
            value = getattr(it, "value", it)
            if isinstance(value, dict) and value.get("type") == "approval":
                return value
    except Exception:  # noqa: BLE001
        logger.debug("interrupt 提取失败", exc_info=True)
    return None
