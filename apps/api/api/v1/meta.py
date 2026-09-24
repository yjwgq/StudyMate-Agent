"""健康检查与配置自检。

`/health` 与 `/ready` 分离是刻意设计（设计文档 v1.1 §11.1）：
    /health  存活探针 —— 只表示进程还在，不触碰任何外部依赖。
             容器编排用它决定「要不要重启进程」。
    /ready   就绪探针 —— M0 只查 LLM 配置；M1 起加入 DB / Redis 连通性
             与 JWT 配置，决定「要不要把流量放进来」。依赖没配好时
             返回 503 + problems 列表，但不重启进程（重启也没用）。
"""

import json
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from apps.api.core.config import settings
from apps.api.core.db import check_db, session_scope
from apps.api.core.errors import AppError, ErrorCode
from apps.api.core.redis import check_redis, get_redis

logger = logging.getLogger(__name__)

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
        "rerank_configured": settings.rerank_configured,
        "rerank_model": settings.rerank_model or "(未配置)",
        "hybrid_top_k": settings.hybrid_top_k,
        "rrf_k": settings.rrf_k,
    }


# ---------------- 检索 feature flag（M6，§14.3） ----------------


class FlagUpdate(BaseModel):
    name: str = Field(description="flag 名，如 retrieval.rerank.enabled")
    value: bool | None = Field(
        default=None, description="true/false 写入覆写；null 清除覆写（回到配置默认）"
    )


def _require_dev() -> None:
    """flag 是**全局**开关，开放给普通用户等于给所有人关掉检索能力。

    因此只在开发环境暴露（生产经 .env 配置或运维通道变更）。
    """
    if settings.app_env != "dev":
        raise AppError(ErrorCode.FORBIDDEN, "feature flag 仅在 dev 环境可变更", 403)


@router.get("/flags")
async def get_flags_endpoint() -> dict[str, object]:
    """当前生效的检索 flag（含来源：配置默认 / Redis 覆写）。

    只读接口不限制环境 —— 验收与评测需要确认「开关真的生效了」。
    """
    from agent.retrieval.flags import get_flags

    flags = await get_flags(get_redis(), use_cache=False)
    return {
        "data": {
            "flags": flags.as_dict(),
            "overridden": list(flags.overridden),
        }
    }


@router.post("/flags")
async def set_flag_endpoint(req: FlagUpdate) -> dict[str, object]:
    """写入/清除 flag 覆写（dev only）；变更落 audit_logs（§13.12）。"""
    from agent.retrieval.flags import get_flags, is_known_flag, set_flag

    _require_dev()
    if not is_known_flag(req.name):
        raise AppError(ErrorCode.VALIDATION, f"未知 flag：{req.name}", 422)
    redis = get_redis()
    await set_flag(redis, req.name, req.value)
    try:
        async with session_scope() as session:
            await session.execute(
                text("""
                    INSERT INTO audit_logs (user_id, actor_type, action, target, payload)
                    VALUES (NULL, 'system', 'flag.changed', :target, CAST(:payload AS JSONB))
                """),
                {
                    "target": req.name,
                    "payload": json.dumps({"value": req.value}, ensure_ascii=False),
                },
            )
    except Exception:  # noqa: BLE001 —— 审计失败不改变开关结果
        logger.warning("flag 变更审计写入失败", exc_info=True)

    flags = await get_flags(redis, use_cache=False)
    return {"data": {"flags": flags.as_dict(), "overridden": list(flags.overridden)}}
