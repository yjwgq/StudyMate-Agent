"""chat 依赖（M5 起从 chat.py 拆出，保持 M1 的测试注入契约）。

get_llm_client 保留在独立模块：security 测试以
`app.dependency_overrides[get_llm_client]` 注入 FakeLLM，
函数对象身份必须稳定（不能随 chat.py 重构漂移）。
"""

from typing import Any

from apps.api.core.config import settings
from apps.api.core.errors import AppError, ErrorCode


async def get_llm_client() -> Any:
    """LLM 客户端工厂（M1 契约保留：测试用 dependency_overrides 注入 fake）。"""
    if not settings.llm_configured:
        raise AppError(
            ErrorCode.LLM_OFFLINE,
            "LLM 未配置：请在 .env 中填写 LLM_API_KEY 与 LLM_MODEL",
            503,
        )
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout_s,
    )


# ---------------- 工具注册表（进程级单例，MCP 惰性发现） ----------------

_registry: Any = None
_registry_lock: Any = None
_mcp_pool: Any = None


async def get_tool_registry() -> Any:
    """工具注册表单例（M4 起从 chat.py 移入；chat_runtime 与端点共用）。"""
    global _registry, _registry_lock, _mcp_pool
    import asyncio
    import logging

    from agent.tools.builtin.retrieval_tool import build_builtin_tools
    from agent.tools.policy import bootstrap_policies
    from agent.tools.registry import ToolRegistry

    logger = logging.getLogger(__name__)
    if _registry is not None:
        return _registry
    if _registry_lock is None:
        _registry_lock = asyncio.Lock()
    async with _registry_lock:
        if _registry is not None:
            return _registry
        bootstrap_policies()
        registry = ToolRegistry(redis=None)  # redis 由 run_turn_task 注入
        for tool in build_builtin_tools():
            registry.register(tool)
        try:
            from agent.tools.mcp_client import McpClientPool, McpServerSpec

            _mcp_pool = McpClientPool([
                McpServerSpec("sandbox", "mcp_servers.sandbox_server", timeout_s=10.0),
                McpServerSpec("todo", "mcp_servers.todo_server", timeout_s=10.0),
                McpServerSpec("search", "mcp_servers.search_server", timeout_s=12.0),
                McpServerSpec("email", "mcp_servers.email_server", timeout_s=10.0),
            ])
            for tool in await _mcp_pool.discover():
                try:
                    registry.register(tool)
                except ValueError as exc:
                    logger.warning("MCP 工具 %s 未上架：%s", tool.meta.name, exc)
        except Exception:  # noqa: BLE001
            logger.warning("MCP 工具发现失败，仅内置工具可用", exc_info=True)
        _registry = registry
        return registry
