"""Reranker 客户端单测（M6-3）：响应解析 + 各类失败 → RerankError。

用假 transport 打桩（不发真实网络请求）：精排是**可选增强**，它的每一种
失败都必须可控地变成降级标记，而不是把异常抛给用户 —— 这是 §8.3 的核心，
所以失败路径比成功路径更值得测。
"""

import httpx
import pytest

from agent.provider.rerank import RerankClient, RerankError


def _client(handler) -> RerankClient:
    """构造被测客户端，并把真实 transport 换成 MockTransport。

    注意 headers 要原样带上：生产实现把 Authorization 挂在 client 实例上，
    替身漏掉它会让「鉴权头是否正确」这类断言失去意义。
    """
    c = RerankClient(base_url="https://example.test/rerank", api_key="k", model="m", timeout_s=0.8)
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers=dict(c._client.headers),
    )
    return c


OK_BODY = {
    "output": {
        "results": [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.5},
        ]
    },
    "usage": {"total_tokens": 100},
}


class TestRerankClient:
    async def test_parses_results(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.headers["authorization"] == "Bearer k"
            return httpx.Response(200, json=OK_BODY)

        hits = await _client(handler).rerank("q", ["a", "b", "c"], top_n=2)
        assert [h.index for h in hits] == [2, 0]
        assert hits[0].score == pytest.approx(0.9)

    async def test_top_n_clamped_to_doc_count(self) -> None:
        sent: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json

            sent.update(json.loads(req.content))
            return httpx.Response(200, json=OK_BODY)

        await _client(handler).rerank("q", ["a", "b"], top_n=10)
        assert sent["parameters"]["top_n"] == 2  # min(top_n, len(documents))

    async def test_empty_documents_short_circuits(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("空候选不应发起请求")

        assert await _client(handler).rerank("q", []) == []

    async def test_timeout_becomes_rerank_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("boom")

        with pytest.raises(RerankError, match="超时"):
            await _client(handler).rerank("q", ["a"])

    async def test_http_error_status(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limited")

        with pytest.raises(RerankError, match="429"):
            await _client(handler).rerank("q", ["a"])

    async def test_malformed_body(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"output": {}})

        with pytest.raises(RerankError, match="响应结构异常"):
            await _client(handler).rerank("q", ["a"])

    async def test_empty_results(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"output": {"results": []}})

        with pytest.raises(RerankError, match="空结果"):
            await _client(handler).rerank("q", ["a"])

    async def test_network_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns fail")

        with pytest.raises(RerankError, match="网络错误"):
            await _client(handler).rerank("q", ["a"])
