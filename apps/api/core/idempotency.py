"""API 层幂等（设计文档 v1.1 §7.6 第一层）。

机制：前端为每次用户输入生成 `Idempotency-Key`（UUID）；
服务端在 Redis 存 `idem:{user_id}:{sha256(key)} → 结果记录`，TTL 24h。

状态机：
    absent      首次请求 → SET NX 占位 {status: in_progress} → 正常执行
    in_progress 同 key 的并发重复请求 → 409 CONFLICT + Retry-After（区别于会话锁的语义，但表现一致）
    completed   已完成 → 直接重放：不再调用模型、不再落库（B7 验收）
    failed      上次执行失败 → 重放失败信息（前端可生成新 key 重试）

工具层 / 存储层幂等在 M4 / M2 分别落地（dedupe_key 与 DB UNIQUE 约束）。
"""

import hashlib
import json
from typing import Any

from redis.asyncio import Redis

from apps.api.core.config import settings

_TTL_S = settings.idempotency_ttl_s


def _key(user_id: str, raw_key: str) -> str:
    digest = hashlib.sha256(raw_key.encode()).hexdigest()
    return f"idem:{user_id}:{digest}"


async def acquire(
    redis: Redis, user_id: str, raw_key: str
) -> tuple[str, dict[str, Any] | None]:
    """尝试占位。返回 (状态, 记录)：

    - ("acquired", None)     首次请求，已写入 in_progress 占位
    - ("completed", payload) 已有完成记录 → 调用方直接重放
    - ("failed", payload)    上次失败 → 调用方重放失败事件
    - ("in_progress", None)  同 key 正在执行 → 调用方返回 409
    """
    record = {"status": "in_progress", "content": "", "conversation_id": "", "usage": None}
    ok = await redis.set(
        _key(user_id, raw_key),
        json.dumps(record, ensure_ascii=False),
        nx=True,
        ex=_TTL_S,
    )
    if ok:
        return "acquired", None

    raw = await redis.get(_key(user_id, raw_key))
    if raw is None:
        # 刚好过期 / 被清理：按首次处理
        ok = await redis.set(
            _key(user_id, raw_key),
            json.dumps(record, ensure_ascii=False),
            nx=True,
            ex=_TTL_S,
        )
        if ok:
            return "acquired", None
    payload = json.loads(raw) if raw else None
    status = (payload or {}).get("status", "in_progress")
    return status, payload


async def peek(
    redis: Redis, user_id: str, raw_key: str
) -> tuple[str, dict[str, Any] | None]:
    """只读检查（**不写占位**）。返回 (状态, 记录)：

    - ("absent", None)       无记录 → 调用方继续正常流程
    - ("completed", payload) 已完成 → 调用方直接重放
    - ("failed", payload)    上次失败 → 调用方重放失败事件
    - ("in_progress", ...)   同 key 正在执行 → 不在此判定，
                             并发冲突统一由锁 / acquire 处理

    为什么必须与 acquire 分开：请求早期只应「查」，若在拿会话锁之前
    就写入 in_progress 占位，同一请求走到锁内正式占位时会撞上自己
    写下的占位，所有带 Idempotency-Key 的新请求都必然 409。
    """
    raw = await redis.get(_key(user_id, raw_key))
    if raw is None:
        return "absent", None
    payload = json.loads(raw)
    return payload.get("status", "in_progress"), payload


async def finish(
    redis: Redis,
    user_id: str,
    raw_key: str,
    *,
    status: str,
    content: str = "",
    conversation_id: str = "",
    usage: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    """写入终态（completed / failed / interrupted），TTL 从写入起重新计 24h。"""
    payload: dict[str, Any] = {"status": status, "content": content, "conversation_id": conversation_id, "usage": usage}
    if error:
        payload["error"] = error
    await redis.set(
        _key(user_id, raw_key),
        json.dumps(payload, ensure_ascii=False),
        ex=_TTL_S,
    )
