"""Embedding Provider（M2-7，ADR-8：全程云端，不部署本地模型）。

OpenAI 兼容接口指向 DashScope 兼容模式（.env: EMBED_BASE_URL/MODEL/KEY）。

要点：
    - 维度运行时校验：首次成功响应就检查 len(vector) == EMBED_DIM，
      不一致立即失败 —— 维度与 DDL 的 vector(1024) 不符时，
      **必须在入库前**拦截，否则全量重嵌入（ADR-8）；
    - 批量：单请求最多 embed_batch_size 条（DashScope 兼容模式上限 10）；
    - 重试：429 / 5xx / 连接错误按指数退避重试，重试耗尽抛 EmbeddingError；
    - embed_all 提供逐批回调 on_batch，供 ingest 管线逐批落库（断点续传）。
"""

import logging
import time

from openai import OpenAI

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """调用失败（重试耗尽 / 配置错误）。"""


class EmbeddingDimError(EmbeddingError):
    """返回维度与 EMBED_DIM 不一致 —— 入库前拦截（ADR-8）。"""


class EmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        batch_size: int = 10,
        max_retries: int = 3,
    ) -> None:
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self.max_retries = max_retries
        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._dim_verified = False

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """单批（≤ batch_size 条）嵌入，带重试。失败抛 EmbeddingError。"""
        if not texts:
            return []
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.embeddings.create(model=self.model, input=list(texts))
                vectors = [d.embedding for d in resp.data]
                if len(vectors) != len(texts):
                    raise EmbeddingError(
                        f"embedding 返回条数不符：请求 {len(texts)}，返回 {len(vectors)}"
                    )
                if not self._dim_verified:
                    got = len(vectors[0])
                    if got != self.dim:
                        raise EmbeddingDimError(
                            f"Embedding 维度不符：模型返回 {got}，EMBED_DIM={self.dim}。"
                            "必须改回一致的模型/维度或调整 DDL 后才能入库（ADR-8）"
                        )
                    self._dim_verified = True
                return vectors
            except EmbeddingDimError:
                raise  # 维度错重试无意义
            except Exception as exc:  # noqa: BLE001 —— 网络类异常统一重试
                last_exc = exc
                if attempt < self.max_retries:
                    wait = 2**attempt
                    logger.warning(
                        "embed retry %s/%s after %ss: %s",
                        attempt + 1, self.max_retries, wait, exc,
                    )
                    time.sleep(wait)
        raise EmbeddingError(f"embedding 调用失败（重试 {self.max_retries} 次后）：{last_exc}")

    def embed_all(
        self,
        texts: list[str],
        *,
        on_batch=None,
    ) -> list[list[float]]:
        """全量分批嵌入。

        on_batch(start_idx, vectors)：每批成功后回调（ingest 管线在此逐批落库，
        实现断点续传）。返回与 texts 等长的向量列表。
        """
        out: list[list[float]] = [[] for _ in texts]
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            vectors = self.embed_batch(batch)
            for i, v in enumerate(vectors):
                out[start + i] = v
            if on_batch is not None:
                on_batch(start, vectors)
        return out
