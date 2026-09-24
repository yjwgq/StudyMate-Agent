"""检索链路的 feature flag（M6，设计文档 §14.3）。

设计取舍（§14.3 原文：「轻量 feature flag（配置文件 + Redis 覆写，无需引入 Unleash）」）：
    - **配置文件**给默认值（`settings.retrieval_*`）——生产默认开混合检索与精排；
    - **Redis 覆写**给运行时开关（key = `flag:retrieval.hybrid.enabled` 等）——
      评测（G1 两套管线对比）与降级验收（G2/G3 模拟依赖故障）都用它切换，
      **不依赖真实故障注入、不需要重新部署**；
    - **进程内缓存 5s**：检索在热路径上，每次查询都读 8 个 Redis key 属读放大；
      5s 的陈旧窗口对「评测切换」与「故障演练」完全够用（切换后等一拍即可）。

为什么集群安全：覆写值存在 Redis（所有副本共享），缓存只在进程内做短时桶。
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# flag 名 → 配置默认值属性名（.env 用 FLAG 名的大写下划线形式，见 .env.example）
FLAG_SPEC: dict[str, str] = {
    "retrieval.hybrid.enabled": "retrieval_hybrid_enabled",
    "retrieval.rerank.enabled": "retrieval_rerank_enabled",
    "retrieval.vector.enabled": "retrieval_vector_enabled",
    "retrieval.keyword.enabled": "retrieval_keyword_enabled",
}

_CACHE_TTL_S = 5.0

# 配置属性名 → RetrievalFlags 字段名
_SHORT = {
    "retrieval_hybrid_enabled": "hybrid",
    "retrieval_rerank_enabled": "rerank",
    "retrieval_vector_enabled": "vector",
    "retrieval_keyword_enabled": "keyword",
}


@dataclass(frozen=True)
class RetrievalFlags:
    """一次检索的生效开关（含来源标注，供调试台与验收追溯）。"""

    hybrid: bool = True
    rerank: bool = True
    vector: bool = True
    keyword: bool = True
    overridden: tuple[str, ...] = ()  # 被 Redis 覆写的 flag 名（排障用）

    def as_dict(self) -> dict[str, bool]:
        return {
            "retrieval.hybrid.enabled": self.hybrid,
            "retrieval.rerank.enabled": self.rerank,
            "retrieval.vector.enabled": self.vector,
            "retrieval.keyword.enabled": self.keyword,
        }


_cache: tuple[float, RetrievalFlags] | None = None


def _defaults() -> RetrievalFlags:
    from apps.api.core.config import settings

    return RetrievalFlags(
        hybrid=settings.retrieval_hybrid_enabled,
        rerank=settings.retrieval_rerank_enabled,
        vector=settings.retrieval_vector_enabled,
        keyword=settings.retrieval_keyword_enabled,
    )


def _parse_bool(raw: Any) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


async def get_flags(redis: Any = None, *, use_cache: bool = True) -> RetrievalFlags:
    """读取生效 flag：配置默认 + Redis 覆写。

    redis 为 None（脚本 / 单测 / 无 Redis 环境）时只返回配置默认值 ——
    调用方不必关心 Redis 是否可用。
    """
    global _cache
    now = time.monotonic()
    if use_cache and _cache is not None and now - _cache[0] < _CACHE_TTL_S:
        return _cache[1]

    base = _defaults()
    effective: dict[str, bool] = {
        "hybrid": base.hybrid, "rerank": base.rerank,
        "vector": base.vector, "keyword": base.keyword,
    }
    overridden: list[str] = []
    if redis is not None:
        for name, attr in FLAG_SPEC.items():
            try:
                value = _parse_bool(await redis.get(f"flag:{name}"))
            except Exception:  # noqa: BLE001 —— Redis 故障时退回默认值（fail-safe）
                logger.warning("读取 flag 覆写失败：%s", name, exc_info=True)
                value = None
            if value is not None:
                effective[_SHORT[attr]] = value
                overridden.append(name)
    flags = RetrievalFlags(
        hybrid=effective["hybrid"], rerank=effective["rerank"],
        vector=effective["vector"], keyword=effective["keyword"],
        overridden=tuple(overridden),
    )
    _cache = (now, flags)
    return flags


async def set_flag(redis: Any, name: str, value: bool | None) -> None:
    """写入/清除覆写（value=None 清除，回到配置默认）。仅 dev 环境经 API 暴露。"""
    key = f"flag:{name}"
    if value is None:
        await redis.delete(key)
    else:
        await redis.set(key, "1" if value else "0")
    reset_cache()


def reset_cache() -> None:
    """清进程内缓存（测试与写入后立即生效用）。"""
    global _cache
    _cache = None


def is_known_flag(name: str) -> bool:
    return name in FLAG_SPEC
