"""健康检查与配置自检。

`/health` 与 `/ready` 分离是刻意设计（设计文档 v1.1 §11.1）：
    /health  存活探针 —— 只表示进程还在，不触碰任何外部依赖。
             容器编排用它决定「要不要重启进程」。
    /ready   就绪探针 —— M0 只查 LLM 配置；M1 起加入 DB / Redis 连通性
             与 JWT 配置，决定「要不要把流量放进来」。依赖没配好时
             返回 503 + problems 列表，但不重启进程（重启也没用）。
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from apps.api.core.config import settings
from apps.api.core.db import check_db
from apps.api.core.redis import check_redis

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict[str, str]:
    """存活探针：不查任何外部依赖。"""
    return {"status": "ok", "app_env": settings.app_env}


@router.get("/ready")
async def ready() -> JSONResponse:
    """就绪探针：LLM 配置 + JWT 配置 + PostgreSQL + Redis。"""
    problems: list[str] = []

    if not settings.llm_configured:
        problems.append("LLM_API_KEY / LLM_MODEL 未配置")
    if not settings.jwt_configured:
        problems.append("JWT_SECRET 未配置（openssl rand -hex 32）")

    db_err = await check_db()
    if db_err:
        problems.append(db_err)

    redis_err = await check_redis()
    if redis_err:
        problems.append(redis_err)

    if problems:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "problems": problems},
        )
    return JSONResponse(content={"status": "ready"})


@router.get("/config")
async def config_summary() -> dict[str, object]:
    """调试用：确认配置读取正确。只回显非敏感字段。"""
    return {
        "app_env": settings.app_env,
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model or "(未配置)",
        "llm_api_key_set": bool(settings.llm_api_key),
        "llm_timeout_s": settings.llm_timeout_s,
        "db_configured": settings.db_configured,
        "redis_url": settings.redis_url.split("@")[-1] if settings.redis_url else "(未配置)",
        "jwt_secret_set": settings.jwt_configured,
        "access_ttl_min": settings.access_ttl_min,
        "refresh_ttl_days": settings.refresh_ttl_days,
    }
