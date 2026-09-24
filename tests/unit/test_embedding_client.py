"""Embedding 客户端单测（M2-7）：fake OpenAI 传输层，不真调 API。"""

import pytest

from agent.provider.embedding import EmbeddingClient, EmbeddingDimError, EmbeddingError


class _Resp:
    def __init__(self, vectors):
        self.data = [type("D", (), {"embedding": v})() for v in vectors]


def _client(dim=4, **kw):
    return EmbeddingClient(
        base_url="http://fake", api_key="k", model="m", dim=dim,
        batch_size=2, max_retries=kw.pop("max_retries", 1), **kw,
    )


def test_dim_mismatch_fails_fast(monkeypatch):
    """维度不符 → 立即失败（入库前拦截，ADR-8），且不重试。"""
    c = _client(dim=4)
    calls = []

    def fake_create(**kwargs):
        calls.append(1)
        return _Resp([[0.1] * 8])          # 返回 8 维 ≠ 4

    monkeypatch.setattr(c._client.embeddings, "create", fake_create)
    with pytest.raises(EmbeddingDimError):
        c.embed_batch(["x"])
    assert len(calls) == 1                  # 无重试


def test_batching_and_order(monkeypatch):
    c = _client(dim=2)
    seen = []

    def fake_create(**kwargs):
        inp = kwargs["input"]
        seen.append(list(inp))
        return _Resp([[float(len(inp)), 0.0]] * len(inp))

    monkeypatch.setattr(c._client.embeddings, "create", fake_create)
    out = c.embed_all(["a", "b", "c", "d", "e"], on_batch=lambda s, v: None)
    assert seen == [["a", "b"], ["c", "d"], ["e"]]   # batch_size=2
    assert len(out) == 5


def test_retry_then_success(monkeypatch):
    c = _client(dim=2, max_retries=2)
    attempts = []

    def fake_create(**kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("boom")
        return _Resp([[0.5, 0.5]])

    monkeypatch.setattr(c._client.embeddings, "create", fake_create)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert c.embed_batch(["x"]) == [[0.5, 0.5]]
    assert len(attempts) == 3


def test_retries_exhausted_raises(monkeypatch):
    c = _client(dim=2, max_retries=1)

    def fake_create(**kwargs):
        raise ConnectionError("down")

    monkeypatch.setattr(c._client.embeddings, "create", fake_create)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(EmbeddingError):
        c.embed_batch(["x"])
