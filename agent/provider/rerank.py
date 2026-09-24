"""Reranker Provider（M6-3，§8.2 / ADR-8）。

DashScope 原生 text-rerank 端点（**不是** OpenAI 兼容的 /compatible-mode/v1）：
    POST {RERANK_BASE_URL}
    Authorization: Bearer {RERANK_API_KEY}
    {"model": ..., "input": {"query": ..., "documents": [...]},
     "parameters": {"return_documents": false, "top_n": N}}
    → {"output": {"results": [{"index": 0, "relevance_score": 0.78}, ...]},
       "usage": {"total_tokens": 293}}

实测（M6 第一步）：50 候选、top_n=6，延迟中位 185ms（首次 354ms），
响应按 relevance_score 降序返回 —— 契合 §8.3 的 800ms 超时上限。

ADR-8 第 4 条：**reranker 分数的绝对值依赖具体模型**，引用相关性阈值必须
按模型分别标定。本项目实际用 `qwen3.7-text-rerank`（设计文档早期写的
`bge-reranker-v2-m3` 是同类能力的另一实现），故阈值一律以本模型实测为准。

失败即降级（§8.3）：任何异常（超时/网络/限流/响应结构异常）抛 RerankError，
由检索层转成 `degraded:rerank` 并用 RRF 顺序 —— 精排是**可选增强**，
它的失败绝不能阻断回答。
"""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


class RerankError(Exception):
    """精排调用失败（超时 / 网络 / 限流 / 响应异常）→ 上层降级。"""


@dataclass
class RerankHit:
    index: int            # 对应入参 documents 的下标
    score: float          # relevance_score（0~1，模型相关，不可跨模型比较）


class RerankClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 0.8,
    ) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(0.5, timeout_s)),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        self._url = base_url

    async def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[RerankHit]:
        """对候选文档精排，返回按分数降序的结果（最多 top_n 条）。

        超时/失败抛 RerankError —— 调用方负责降级（§8.3）。
        """
        if not documents:
            return []
        payload: dict = {
            "model": self.model,
            "input": {"query": query, "documents": documents},
            "parameters": {"return_documents": False},
        }
        if top_n is not None:
            payload["parameters"]["top_n"] = min(top_n, len(documents))
        try:
            resp = await self._client.post(self._url, json=payload)
        except httpx.TimeoutException as exc:
            raise RerankError(f"精排超时（>{self.timeout_s}s）") from exc
        except httpx.HTTPError as exc:
            raise RerankError(f"精排网络错误：{exc}") from exc

        if resp.status_code != 200:
            raise RerankError(f"精排返回 {resp.status_code}：{resp.text[:200]}")

        try:
            data = resp.json()
            results = data["output"]["results"]
            hits = [
                RerankHit(index=int(r["index"]), score=float(r["relevance_score"]))
                for r in results
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankError(f"精排响应结构异常：{exc}") from exc
        if not hits:
            raise RerankError("精排返回空结果")
        return hits

    async def aclose(self) -> None:
        await self._client.aclose()


_client: RerankClient | None = None


def get_rerank_client() -> RerankClient | None:
    """进程级单例；未配置返回 None（调用方直接跳过精排并标记降级）。"""
    global _client
    from apps.api.core.config import settings

    if _client is None:
        if not settings.rerank_configured:
            logger.info("rerank 未配置（RERANK_* 缺失），精排路径将显式降级")
            return None
        _client = RerankClient(
            base_url=settings.rerank_base_url,
            api_key=settings.rerank_api_key,
            model=settings.rerank_model,
            timeout_s=settings.rerank_timeout_s,
        )
    return _client
