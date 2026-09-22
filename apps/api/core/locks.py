"""会话级并发锁（设计文档 v1.1 §7.4）。

同一 conversation 的执行状态是串行独占的（thread_id = conversation_id）。
并发第二条消息 → 409 CONFLICT + Retry-After；前端本就在 loading 态。

实现细节：
    - SET NX PX：原子「不存在才写入 + 过期时间」，锁自带 5 分钟 TTL 兜底，
      持有者崩溃也不会永久堵死会话（正常路径结束时显式释放）；
    - 释放用 Lua「比较 token 再删」：防止把别人（TTL 到期后重新拿锁的请求）
      的锁误删 —— 判断 + 删除必须原子。
"""

import uuid

from redis.asyncio import Redis

from apps.api.core.config import settings

# 仅当 value 与本请求的 token 一致时才删除
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _key(conversation_id: str) -> str:
    return f"conversation_lock:{conversation_id}"


async def acquire_conversation_lock(redis: Redis, conversation_id: str) -> str | None:
    """尝试获取锁。成功返回本次持有 token（用于释放比对）；被占用返回 None。"""
    token = uuid.uuid4().hex
    ok = await redis.set(
        _key(conversation_id),
        token,
        nx=True,
        px=settings.conversation_lock_ttl_ms,
    )
    return token if ok else None


async def release_conversation_lock(redis: Redis, conversation_id: str, token: str) -> None:
    """释放自己持有的锁（非阻塞；lua 保证「比对 + 删除」原子性）。"""
    await redis.eval(_RELEASE_LUA, 1, _key(conversation_id), token)
