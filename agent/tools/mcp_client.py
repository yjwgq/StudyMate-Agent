"""MCP 客户端池与工具适配（M4-9，ADR-4 / §13.10）。

McpClientPool（每个 server 一个 stdio 会话）：
    - 惰性拉起：第一次调用时 spawn `python -m mcp_servers.<name>`；
    - 工具列表缓存 + 重启后校验（列表变化 → 告警，防供应链篡改，§13.10 第 5 条）；
    - 调用超时（进程级兜底；meta.timeout_s 的统一超时在 ToolRegistry 层）；
    - 崩溃自动重启一次，再失败交由上层报错。

McpTool（BaseTool 适配器）：
    - meta.source="mcp"，risk_level 由本地 policy 注册表覆盖（不信任自述）；
    - injected_params 剔除在 LLM schema 之外，由 registry 注入 user_id 等；
    - MCP 返回 JSON 文本 → ToolResult。
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import Field, create_model

from agent.tools.base import BaseTool, ToolArgs, ToolCtx, ToolMeta, ToolResult

logger = logging.getLogger(__name__)


class McpServerSpec:
    def __init__(self, name: str, module: str, *, timeout_s: float = 15.0) -> None:
        self.name = name
        self.module = module  # python -m <module>
        self.timeout_s = timeout_s


class McpSession:
    """一个 server 的会话管理（**按调用开短会话**）。

    为什么不用长生命周期 stdio 会话：anyio 的 cancel scope 与进入它的任务
    绑定 —— API 进程里首个请求的任务建立会话后，后续请求（不同任务）使用/
    退出会触发「cancel scope in a different task」运行时错误（M4 验收实锤，
    表现为流被随机 500）。短会话（spawn → call → close）在同一任务内完成，
    代价是每次调用约 100–300ms 的进程拉起开销 —— M4 工具调用频次低，可接受；
    M7 若成瓶颈再引入 supervisor-task 模式。
    """

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self._tool_names: list[str] = []  # 跨调用缓存，用于重启后校验（§13.10 第 5 条）

    async def _open(self) -> tuple[AsyncExitStack, ClientSession]:
        import os
        import sys

        stack = AsyncExitStack()
        # env 必须显式传：SDK 的 env=None 是「最小白名单环境」而不是继承，
        # 子进程会拿不到 DATABASE_URL_WORKER（M4 验收实锤）
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", self.spec.module],
            env=dict(os.environ),
        )
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return stack, session
        except Exception:
            await stack.aclose()
            raise

    async def _listed(self, session: ClientSession) -> list[Any]:
        listed: Any = await session.list_tools()
        return list(getattr(listed, "tools", None) or [])

    async def restart(self) -> None:
        """短会话模型下无常驻进程可重启：仅清缓存（保持接口兼容）。"""
        self._tool_names = []

    async def call(self, tool: str, args: dict[str, Any], timeout_s: float) -> str:
        last_err: Exception | None = None
        for attempt in (1, 2):  # 失败重开一次会话
            stack: AsyncExitStack | None = None
            try:
                stack, session = await self._open()
                tools = await self._listed(session)
                names = sorted(t.name for t in tools)
                if self._tool_names and names != self._tool_names:
                    logger.warning(
                        "MCP server %s 工具列表变化: %s -> %s（供应链篡改信号，§13.10）",
                        self.spec.name, self._tool_names, names,
                    )
                self._tool_names = names
                result = await asyncio.wait_for(session.call_tool(tool, args), timeout=timeout_s)
                text = _content_to_text(result)
                # fastmcp 把工具内部异常包成 isError 结果 —— 不检查会把错误
                # 文本当成功结果回灌给模型（M4 验收实锤）
                if getattr(result, "isError", False):
                    raise RuntimeError(f"MCP 工具执行错误: {text[:300]}")
                return text
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("MCP call %s.%s 失败（尝试 %s）: %s", self.spec.name, tool, attempt, exc)
            finally:
                if stack is not None:
                    await stack.aclose()
        raise RuntimeError(f"MCP {self.spec.name}.{tool} 调用失败: {last_err}") from last_err

    async def list_tools_once(self) -> list[Any]:
        """开一次性会话取工具列表（discover 用）。"""
        stack, session = await self._open()
        try:
            return await self._listed(session)
        finally:
            await stack.aclose()

    async def close(self) -> None:
        self._tool_names = []


def _content_to_text(result: Any) -> str:
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


class McpTool(BaseTool):
    """把 MCP server 上的一个工具适配成本地 BaseTool。

    双向名字：meta.name 是本地名（对齐 policy 注册表，如 todo），
    _remote_name 是 MCP server 端原名（如 create_todo）—— 调用必须用原名。
    """

    def __init__(
        self, session: McpSession, name: str, description: str, args_schema: type[ToolArgs],
        *, remote_name: str,
    ) -> None:
        self._session = session
        self._name = name
        self._remote_name = remote_name
        self.meta = ToolMeta(
            name=name,
            description=(description or f"MCP tool {name}")[:500],  # §13.10 描述净化（限长）
            args_schema=args_schema,
            risk_level=2,  # 自述仅兜底；实际等级以本地 policy 注册表覆盖为准
            idempotent=False,
            timeout_s=session.spec.timeout_s,
            source="mcp",
            mcp_server=session.spec.name,
        )

    async def arun(self, ctx: ToolCtx, **kwargs: Any) -> ToolResult:
        import time

        started = time.perf_counter()
        text = await self._session.call(self._remote_name, kwargs, self.meta.timeout_s)
        return ToolResult(
            ok=True,
            content=text,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


# MCP 工具名 → 本地名（对齐 policy 注册表与提示词习惯）
NAME_MAP = {
    ("sandbox", "run_python"): "sandbox",
    ("todo", "create_todo"): "todo",
    ("todo", "list_todos"): "todo_list",
    ("todo", "complete_todo"): "todo_complete",
    ("search", "web_search"): "search",
    ("email", "send_email"): "send_email",
    ("email", "list_outbox"): "list_outbox",
}

# 治理层注入参数（LLM schema 剔除；registry.invoke 时由 ToolCtx 填真值）
INJECTED_BY_TOOL = {
    "todo": ["user_id", "dedupe_key"],
    "todo_list": ["user_id"],
    "todo_complete": ["user_id"],
}


class McpClientPool:
    """管理全部 MCP server；从 tools/list 生成 McpTool。"""

    def __init__(self, specs: list[McpServerSpec]) -> None:
        self.sessions = {s.name: McpSession(s) for s in specs}

    async def discover(self) -> list[McpTool]:
        """取全部 server 的工具并适配。单个 server 挂了不拖垮其余（降级显式）。"""
        tools: list[McpTool] = []
        for server_name, session in self.sessions.items():
            try:
                listed = await session.list_tools_once()
            except Exception:  # noqa: BLE001
                logger.warning("MCP server %s 发现工具失败", server_name, exc_info=True)
                continue
            for t in listed:
                local_name = NAME_MAP.get((server_name, t.name), f"{server_name}_{t.name}")
                schema = _args_model_from(t.inputSchema, name=f"{local_name}_args")
                tool = McpTool(
                    session, local_name, t.description or "", schema, remote_name=t.name
                )
                tool.meta.injected_params = INJECTED_BY_TOOL.get(local_name, [])
                tools.append(tool)
        return tools

    async def close(self) -> None:
        for session in self.sessions.values():
            await session.close()


def _args_model_from(input_schema: dict[str, Any], *, name: str) -> type[ToolArgs]:
    """从 MCP JSON schema 动态生成 pydantic 模型。

    支持基础类型 + 数组（`{"type": "array", "items": {...}}` → list[T]）——
    M5 验收实锤：`send_email(to=[...])` 的数组参数被旧映射当成 str，
    参数校验直接失败（模型根本调不动工具）。
    """
    props = (input_schema or {}).get("properties", {}) or {}
    required = set((input_schema or {}).get("required", []) or [])
    type_map: dict[str, Any] = {"string": str, "integer": int, "number": float, "boolean": bool}
    field_defs: dict[str, Any] = {}
    for fname, spec in props.items():
        raw_type = str(spec.get("type", "string"))
        if raw_type == "array":
            items = spec.get("items") or {}
            item_type = type_map.get(str(items.get("type", "string")), str)
            py_type: Any = list[item_type]  # type: ignore[valid-type]
        else:
            py_type = type_map.get(raw_type, str)
        desc = str(spec.get("description", ""))[:200]
        if fname in required:
            field_defs[fname] = (py_type, Field(..., description=desc))
        else:
            field_defs[fname] = (py_type | None, Field(default=None, description=desc))
    return create_model(name, __base__=_DynamicArgs, **field_defs)


class _DynamicArgs(ToolArgs):
    """MCP 工具参数基类。字段级校验在 MCP server 侧；这里做类型兜底。"""
