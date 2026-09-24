"""检索 feature flag 单测（M6，§14.3）：配置默认 + Redis 覆写 + 缓存。

flag 是「评测切管线」与「G2/G3 模拟故障」的唯一开关，错了会让整轮验收失真
（例如覆写没生效 → 以为测的是混合检索、实际还是单路）。因此覆盖：
    优先级（覆写 > 配置默认）、清除覆写、Redis 故障时 fail-safe、缓存与失效。
"""

import pytest

from agent.retrieval import flags as flags_mod
from agent.retrieval.flags import RetrievalFlags, get_flags, is_known_flag, set_flag


class FakeRedis:
    def __init__(self, data: dict | None = None, fail: bool = False) -> None:
        self.data = dict(data or {})
        self.fail = fail
        self.writes: list[tuple[str, str | None]] = []

    async def get(self, key: str):
        if self.fail:
            raise RuntimeError("redis down")
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.writes.append((key, value))
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.writes.append((key, None))
        self.data.pop(key, None)


@pytest.fixture(autouse=True)
def _clear_cache():
    flags_mod.reset_cache()
    yield
    flags_mod.reset_cache()


class TestFlags:
    async def test_defaults_without_redis(self) -> None:
        flags = await get_flags(None, use_cache=False)
        # 配置默认：混合检索与精排均开启（M6 起为默认管线）
        assert flags.hybrid is True and flags.rerank is True
        assert flags.vector is True and flags.keyword is True
        assert flags.overridden == ()

    async def test_redis_override_wins(self) -> None:
        redis = FakeRedis({"flag:retrieval.rerank.enabled": "0"})
        flags = await get_flags(redis, use_cache=False)
        assert flags.rerank is False
        assert flags.hybrid is True  # 未覆写的保持默认
        assert "retrieval.rerank.enabled" in flags.overridden

    async def test_override_accepts_various_truthy_forms(self) -> None:
        for raw, expected in [("1", True), ("true", True), ("off", False), (b"yes", True), ("0", False)]:
            redis = FakeRedis({"flag:retrieval.hybrid.enabled": raw})
            flags = await get_flags(redis, use_cache=False)
            assert flags.hybrid is expected, raw

    async def test_unknown_value_ignored(self) -> None:
        """覆写值写成乱码时按「未覆写」处理，而不是崩溃或误判为 True。"""
        redis = FakeRedis({"flag:retrieval.hybrid.enabled": "maybe"})
        flags = await get_flags(redis, use_cache=False)
        assert flags.hybrid is True

    async def test_redis_failure_is_failsafe(self) -> None:
        """Redis 故障 → 退回配置默认（fail-safe），并如实记录 overridden 为空。"""
        flags = await get_flags(FakeRedis(fail=True), use_cache=False)
        assert flags.hybrid is True and flags.overridden == ()

    async def test_set_and_clear_override(self) -> None:
        redis = FakeRedis()
        await set_flag(redis, "retrieval.hybrid.enabled", False)
        assert (await get_flags(redis, use_cache=False)).hybrid is False
        await set_flag(redis, "retrieval.hybrid.enabled", None)  # 清除
        assert (await get_flags(redis, use_cache=False)).hybrid is True

    async def test_cache_hit_avoids_second_read(self) -> None:
        """5s 进程内缓存：热路径上不每次都读 Redis（读放大）。"""
        redis = FakeRedis({"flag:retrieval.rerank.enabled": "0"})
        first = await get_flags(redis)
        assert first.rerank is False
        redis.data["flag:retrieval.rerank.enabled"] = "1"  # 改 Redis
        cached = await get_flags(redis)                    # 命中缓存 → 仍是旧值
        assert cached.rerank is False
        fresh = await get_flags(redis, use_cache=False)    # 绕过缓存 → 新值
        assert fresh.rerank is True

    async def test_set_flag_resets_cache(self) -> None:
        redis = FakeRedis()
        await get_flags(redis)  # 填缓存
        await set_flag(redis, "retrieval.keyword.enabled", False)  # 写入应让缓存失效
        assert (await get_flags(redis)).keyword is False

    def test_known_flag_names(self) -> None:
        assert is_known_flag("retrieval.hybrid.enabled")
        assert not is_known_flag("retrieval.hyde.enabled")  # HyDE 不在 M6 范围

    def test_as_dict_shape(self) -> None:
        d = RetrievalFlags().as_dict()
        assert set(d) == {
            "retrieval.hybrid.enabled", "retrieval.rerank.enabled",
            "retrieval.vector.enabled", "retrieval.keyword.enabled",
        }
