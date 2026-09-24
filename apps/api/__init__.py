"""FastAPI 接入层。

分层约定：
    core/       配置、安全、依赖注入、租户上下文、限流、幂等
    api/v1/     路由（auth / chat / kb / memory / briefing / approval / admin）
    sse.py      SSE 事件协议
"""
