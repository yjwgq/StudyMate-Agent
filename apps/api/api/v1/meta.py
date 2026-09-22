"""健康检查与配置自检。

`/health` 与 `/ready` 分离是刻意设计（设计文档 v1.1 §11.1）：
    /health  存活探针 —— 只表示进程还在，不触碰任何外部依赖。
             容器编排用它决定「要不要重启进程」。
    /ready   就绪探针 —— 检查依赖是否齐备，决定「要不要把流量放进来」。
             依赖没配好时返回 503，但不重启进程（重启也没用）。
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from apps.api.core.config import settings

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict[str, str]:
    """存活探针：不查任何外部依赖。"""
    return {"status": "ok", "app_env": settings.app_env}


@router.get("/ready")
async def ready() -> JSONResponse:
    """就绪探针。

    M0 阶段只检查 LLM 配置（此时还没有数据库）。
    M1 起会加上 PostgreSQL / Redis 连通性检查，对应设计文档 v1.1 §11.1 的完整语义。
    """
    problems: list[str] = []
    if not settings.llm_api_key:
        problems.append("LLM_API_KEY 未配置")
    if not settings.llm_model:
        problems.append("LLM_MODEL 未配置")

    if problems:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "problems": problems},
        )
    return JSONResponse(content={"status": "ready"})


@router.get("/config")
async def config_summary() -> dict[str, object]:
    """调试用：确认配置读取正确。

    只回显非敏感字段 —— `llm_api_key` 仅返回「是否已设置」的布尔值，
    绝不回显内容。这个习惯要从第一个接口就养成。
    """
    return {
        "app_env": settings.app_env,
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model or "(未配置)",
        "llm_api_key_set": bool(settings.llm_api_key),
        "llm_timeout_s": settings.llm_timeout_s,
    }
