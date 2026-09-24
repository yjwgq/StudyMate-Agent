"""MCP server 公共设施（M4-8/9，ADR-4）。

三个 stdio server：sandbox / todo / search。
    - 由 API 容器以子进程方式拉起（mcp_pool.py）；
    - 每个只暴露最小工具面；描述长度受限（§13.10 注入载体）；
    - todo 需要数据库（worker DSN，BYPASSRLS——server 是可信基础设施，
      user_id 由治理层注入，LLM 不可见也不可伪造）。
"""

import logging
import sys

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def make_server(name: str, instructions: str) -> FastMCP:
    app: FastMCP = FastMCP(name, instructions=instructions[:500])
    return app


def run(app: FastMCP) -> None:
    """stdio 入口：compose/本地均以 `python -m mcp_servers.<name>` 拉起。"""
    logging.basicConfig(
        stream=sys.stderr,  # stdout 是 MCP 协议通道，日志必须走 stderr
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app.run(transport="stdio")
