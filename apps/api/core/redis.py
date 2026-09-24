"""Redis 客户端（redis-py 异步版）。

M1 用途：会话级并发锁（§7.4）、API 层幂等（§7.6）。
M9 起还承担 Celery broker 与限流计数。

惰性创建：模块导入期不连接，应用无 Redis 也能启动，由 /ready 报告问题。
"""

from functools import lru_cache

from redis.asyncio import Redis

from apps.api.core.config import settings


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL 未配置")
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,   # 字符串直接返回 str，省去反复 decode
    )


async def check_redis() -> str | None:
    """就绪探针用：PING 通返回 None，失败返回可读原因。"""
    try:
        redis = get_redis()
        if not await redis.ping():
            return "Redis PING 失败"
    except Exception as exc:  # noqa: BLE001 —— 探针要把原因带出去
        return f"Redis 连接失败：{type(exc).__name__}: {exc}"
    return None
