"""Agent 运行时。

按职责分包，各包在对应里程碑落地：

    provider/     LLM / Embedding / Rerank 的统一封装       [M3]
                  三者配置相互独立（ADR-8）：改 LLM 无副作用，
                  改 Embedding 需全量重嵌入

    graph/        LangGraph 图装配、状态定义、节点、路由     [M4]
                  含 planner(DAG) / react / synthesize / reflection / study

    tools/        工具元数据、注册表、风险策略、MCP 客户端    [M4]
                  ToolMeta + BaseTool + ToolRegistry 三层，
                  超时/重试/幂等/风险拦截/埋点全部收敛在 Registry

    memory/       三级记忆（Buffer / 情景 / 语义）与巩固      [M8]
                  含记忆安全校验

    retrieval/    解析、分块、混合检索、精排、引用            [M2–M3, M6]
                  M2 入库管线 · M3 单路检索 · M6 混合检索

    guardrails/   注入检测、PII 脱敏、内容审核、groundedness  [M8–M9]

    callbacks/    Langfuse 追踪与 SSE 事件回调                [M3, M5]
"""
