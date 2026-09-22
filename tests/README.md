测试目录，按设计文档 v1.1 §16 分层：

    unit/         纯函数：分块器、RRF 融合、引用解析、数值规范化、
                  风险分级策略、PII 脱敏、canonical_json（覆盖率要求 ≥80%）
    integration/  Agent 图（fake LLM，不真调模型）、检索管线、
                  HITL 中断恢复（testcontainers 起真实 PG）
    contract/     SSE 事件协议（防止前后端协议漂移）
    security/     跨租户越权、SSRF、注入红队、trace 不含敏感串
    e2e/          Playwright 三条主干流程

其中 security/ 是 CI 必过项，不可跳过。
