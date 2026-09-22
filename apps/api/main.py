"""FastAPI 应用入口。

本地运行：
    uv run uvicorn apps.api.main:app --reload --port 8000

容器内运行：
    uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.api.v1 import chat, meta
from apps.api.core.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(
    title="Personal Agent OS",
    version="0.1.0",
    description="M0：端到端最小闭环（前端 → FastAPI → DeepSeek → SSE）",
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

app.include_router(meta.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
