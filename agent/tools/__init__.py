"""工具层 —— 由 M4 落地。

三层结构：
    base.py       ToolMeta（元数据）· BaseTool（执行体）· ToolResult
    registry.py   ToolRegistry —— 超时/重试/幂等/风险拦截/结果截断/埋点 的唯一收敛点
    policy.py     本地风险策略注册表（fail-closed：未注册工具默认拒绝）
    builtin/      内置工具
    mcp_client.py MCP 连接、工具发现与生命周期治理

关键约束：风险等级来自本地策略，**不信任 MCP 工具自述**（§13.10）。
"""
