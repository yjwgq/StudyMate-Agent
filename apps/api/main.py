"""FastAPI 应用入口。

本地运行：
    uv run uvicorn apps.api.main:app --reload --port 8000

容器内运行：
    uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
"""

import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from apps.api.api.v1 import auth, chat, conversations, meta
from apps.api.core.config import settings
from apps.api.core.errors import new_trace_id, register_exception_handlers

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(
    title="Personal Agent OS",
    version="0.2.0",
    description="M1：基础设施 + 多租户认证（JWT / RLS / 会话锁 / 幂等）",
)

_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    # 浏览器规范：来源为通配 * 时不允许携带凭据。
    # 这里按配置自动决定，避免上线时踩到这个组合是非法的坑。
    allow_credentials="*" not in _origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """每个请求分配 trace_id：注入 request.state 与响应头，贯穿日志与错误体。"""
    request.state.trace_id = request.headers.get("X-Trace-Id") or new_trace_id()
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - started) * 1000
    response.headers["X-Trace-Id"] = request.state.trace_id
    logging.getLogger("apps.api.access").info(
        "%s %s -> %s %.1fms trace=%s",
        request.method, request.url.path, response.status_code,
        elapsed, request.state.trace_id,
    )
    return response


register_exception_handlers(app)

app.include_router(meta.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(conversations.router, prefix="/api/v1")
