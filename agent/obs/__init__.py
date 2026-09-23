"""可观测性模块（M3，§14.1）。

- redact：PII 脱敏（§13.3）—— 入 Langfuse trace / 日志前必须过这里；
- langfuse：Langfuse Cloud trace 封装，未配置时 no-op，业务零感知。
"""
