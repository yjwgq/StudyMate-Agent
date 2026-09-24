"""自建 MCP 工具服务 —— 由 M4 落地。

    sandbox_server/  RestrictedPython + 子进程隔离 + OS 资源限制
    todo_server/     待办管理（同时用于演示协议定义能力）
    search_server/   Web 搜索

每个 server 独立进程运行，通过 stdio 与主进程通信。
"""
