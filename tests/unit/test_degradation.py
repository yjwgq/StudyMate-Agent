"""降级矩阵单测（M6-4，§8.3）——本里程碑的灵魂。

覆盖「超时/失败 → 显式降级」的每一条分支。用可控的假召回函数驱动真实管线
（`Retriever.search` 的实际编排逻辑），不依赖数据库与网络：

    向量路超时/失败        → 仅关键词路 + degraded:vector
    关键词路超时/失败      → 仅向量路 + degraded:keyword
    两路都挂              → 无结果 + degraded:retrieval（上层据此走"无 RAG"）
    精排失败/超时/未配置/关闭 → 用 RRF 顺序 + degraded:rerank
    向量依赖被开关关闭     → 同上（G3 的开关模拟路径）
"""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from agent.retrieval.flags import RetrievalFlags
from agent.retrieval.search import Retriever

USER = uuid4()


def _row(cid: int, score: float = 0.9, parent: int | None = None) -> dict:
    return {
        "id": uuid4(),
        "parent_id": uuid4() if parent is None else parent,
        "document_id": uuid4(),
        "content": f"child {cid} 内容",
        "meta": {"page": cid},
        "title": "doc.md",
        "score": score,
    }


class FakeSession:
    """占位会话：单测里所有真实 SQL 都被替身绕过。"""

    async def execute(self, *_: Any, **__: Any):  # pragma: no cover
        raise AssertionError("单测不应触达真实 SQL")


@pytest.fixture
def retriever(monkeypatch):
    """可控 Retriever：父块回溯不查库 + 精排默认替换为"成功但不变序"的自旋替身。

    为什么默认注入成功的精排替身：`rerank=False` 按设计**本身就是降级**
    （G2 用开关模拟故障），会往 degraded 里加 "rerank"。召回降级测试要聚焦
    召回分支，故用假精排隔离出这个变量；精排相关用例再单独覆盖替身。
    """
    from agent.provider import rerank as rerank_mod
    from agent.provider.rerank import RerankHit

    r = Retriever()

    async def fake_parents(session, user_id, parent_ids):
        return {pid: f"父块 {pid} 全文" for pid in parent_ids}

    class IdentityClient:
        async def rerank(self, query, documents, *, top_n=None):
            return [RerankHit(index=i, score=1.0 - i * 0.01) for i in range(len(documents))]

    monkeypatch.setattr(r, "_fetch_parents", fake_parents)
    monkeypatch.setattr(rerank_mod, "get_rerank_client", lambda: IdentityClient())
    return r


async def _run(
    retriever, *, flags: RetrievalFlags, vector=None, keyword=None, monkeypatch=None,
    patch_recall: bool = True, patch_keyword: bool = True,
):
    """跑一次 search，用替身控制两路行为（None = 正常返回一行）。

    patch_recall/patch_keyword：不覆盖对应的召回替身 —— 供"用例自己打了替身"
    的场景使用（否则 _run 会把它覆盖掉，分支永远测不到）。
    """
    async def vrec(session, user_id, qvec, k):
        if isinstance(vector, Exception):
            raise vector
        return vector if vector is not None else [_row(1)]

    async def krec(session, user_id, query, k):
        if isinstance(keyword, Exception):
            raise keyword
        return keyword if keyword is not None else [_row(2, score=0.5)]

    if patch_recall:
        monkeypatch.setattr(retriever, "_vector_recall", vrec)
    if patch_recall and patch_keyword:
        monkeypatch.setattr(retriever, "_keyword_recall", krec)
    # 关键词路的独立连接也要绕过（它是真连接）
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_tenant_session(_uid):
        yield FakeSession()

    monkeypatch.setattr("apps.api.core.db.tenant_session", fake_tenant_session)
    return await retriever.search(
        FakeSession(), USER, "监督学习", qvec=[0.1] * 4, flags=flags
    )


@pytest.mark.parametrize(
    "vector,keyword,expect_degraded,expect_hits",
    [
        (None, TimeoutError(), ["keyword"], 1),          # 关键词路超时 → 仅向量
        (TimeoutError(), None, ["vector"], 1),           # 向量路超时 → 仅关键词
        (RuntimeError("boom"), None, ["vector"], 1),     # 向量路异常 → 仅关键词
        # 两路都挂：除各自标记外还要叠整体标记（上层据此走无 RAG 分支）
        (TimeoutError(), TimeoutError(), ["vector", "keyword", "retrieval"], 0),
        (None, None, [], 2),                             # 正常：两路各一条且父块不同
    ],
)
async def test_hybrid_path_degradation(
    retriever, monkeypatch, vector, keyword, expect_degraded, expect_hits
) -> None:
    flags = RetrievalFlags(hybrid=True, rerank=True)  # 精排用替身（不干扰召回断言）
    outcome = await _run(
        retriever, flags=flags, vector=vector, keyword=keyword, monkeypatch=monkeypatch
    )
    assert outcome.degraded == expect_degraded
    assert len(outcome.hits) == expect_hits
    if expect_hits == 0:
        # 两路都挂时还要叠加整体标记（上层据此走无 RAG 分支）
        assert "retrieval" in outcome.degraded or outcome.degraded == ["vector", "keyword"]


async def test_both_down_marks_retrieval(retriever, monkeypatch) -> None:
    flags = RetrievalFlags(hybrid=True, rerank=True)
    outcome = await _run(
        retriever, flags=flags, vector=TimeoutError(), keyword=TimeoutError(),
        monkeypatch=monkeypatch,
    )
    assert "retrieval" in outcome.degraded  # 整体不可用
    assert outcome.hits == []


async def test_vector_disabled_flag_skips_path(retriever, monkeypatch) -> None:
    """G3 的开关模拟路径：向量依赖不可用 → 关键词兜底 + degraded:vector。"""
    flags = RetrievalFlags(hybrid=True, rerank=True, vector=False)
    outcome = await _run(retriever, flags=flags, monkeypatch=monkeypatch)
    assert "vector" in outcome.degraded
    assert outcome.hits  # 关键词路仍然给了结果
    assert outcome.diagnostics["vector_count"] == 0


async def test_rerank_failure_keeps_rrf_order(retriever, monkeypatch) -> None:
    """精排失败 → 降级 + 仍返回结果（顺序退化为 RRF）。"""
    from agent.provider import rerank as rerank_mod

    class FailingClient:
        model = "fake"

        async def rerank(self, query, documents, *, top_n=None):
            raise rerank_mod.RerankError("模拟精排超时")

    monkeypatch.setattr(rerank_mod, "get_rerank_client", lambda: FailingClient())
    flags = RetrievalFlags(hybrid=True, rerank=True)
    vec = [_row(1, score=0.9), _row(3, score=0.7)]
    outcome = await _run(retriever, flags=flags, vector=vec, keyword=[], monkeypatch=monkeypatch)
    assert "rerank" in outcome.degraded
    assert outcome.hits and all(h.rerank_score is None for h in outcome.hits)


async def test_rerank_disabled_flag_marks_degraded(retriever, monkeypatch) -> None:
    """flag 关精排（G2 的模拟路径）：必须标记 degraded:rerank（降级显式）。"""
    flags = RetrievalFlags(hybrid=True, rerank=False)
    outcome = await _run(retriever, flags=flags, monkeypatch=monkeypatch)
    assert "rerank" in outcome.degraded


async def test_rerank_success_fills_score_and_orders(retriever, monkeypatch) -> None:
    """精排成功：候选按精排分重排，rerank_score 落进 Hit（引用脚注要展示）。"""
    from agent.provider import rerank as rerank_mod
    from agent.provider.rerank import RerankHit

    class OkClient:
        async def rerank(self, query, documents, *, top_n=None):
            # 把最后一条排到第一（模拟"精排改变了顺序"）
            return [RerankHit(index=len(documents) - 1, score=0.99),
                    RerankHit(index=0, score=0.11)]

    monkeypatch.setattr(rerank_mod, "get_rerank_client", lambda: OkClient())
    vec = [_row(1, score=0.9), _row(3, score=0.7)]
    flags = RetrievalFlags(hybrid=True, rerank=True)
    outcome = await _run(retriever, flags=flags, vector=vec, keyword=[], monkeypatch=monkeypatch)
    assert not outcome.degraded
    assert outcome.hits[0].rerank_score == pytest.approx(0.99)
    assert outcome.hits[0].score == pytest.approx(0.99)  # 最终排序分用精排分


async def test_single_path_respects_vector_flag(retriever, monkeypatch) -> None:
    """单路管线 + 向量不可用 = 零召回（这正是对比表里 ④ 的语义）。"""
    flags = RetrievalFlags(hybrid=False, rerank=True, vector=False)
    outcome = await _run(retriever, flags=flags, monkeypatch=monkeypatch)
    assert outcome.hits == []
    assert "vector" in outcome.degraded


async def test_timeout_uses_configured_budget(retriever, monkeypatch) -> None:
    """真超时分支：让召回睡过预算，确认 wait_for 把它变成降级而不是抛给上层。"""
    from apps.api.core.config import settings

    monkeypatch.setattr(settings, "retrieval_keyword_timeout_s", 0.05, raising=False)

    async def slow_keyword(session, user_id, query, k):
        await asyncio.sleep(0.5)
        return [_row(9)]

    monkeypatch.setattr(retriever, "_keyword_recall", slow_keyword)
    flags = RetrievalFlags(hybrid=True, rerank=True)
    outcome = await _run(retriever, flags=flags, monkeypatch=monkeypatch, patch_keyword=False)
    assert "keyword" in outcome.degraded
    assert outcome.hits  # 向量路仍在
