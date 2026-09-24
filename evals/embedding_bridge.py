"""评测用 embedding 桥接：复用 v2 的云端 embedding（ADR-8）。

v1 baseline 的 embedding 通道本应使用 Ollama 本地模型，但 v2 已确定
全程云端（ADR-8，本地模式不迁移）。为了让 baseline 与 v2 的对比
只反映「检索策略差异」而非「embedding 提供商差异」，baseline 也走
同一云端模型 —— 这个偏离必须在报告中显式说明（模块 docstring 已注明）。
"""

from agent.provider.embedding import EmbeddingClient
from apps.api.core.config import settings

_client: EmbeddingClient | None = None


def _get_client() -> EmbeddingClient:
    global _client
    if _client is None:
        if not settings.embed_configured:
            raise SystemExit("EMBED_* 未配置，无法评测")
        _client = EmbeddingClient(
            base_url=settings.embed_base_url,
            api_key=settings.embed_api_key,
            model=settings.embed_model,
            dim=settings.embed_dim,
            batch_size=settings.embed_batch_size,
            max_retries=settings.embed_max_retries,
        )
    return _client


def embed_texts(texts: list[str]) -> list[list[float]]:
    """同步接口（Chroma 是同步客户端；评测脚本不在事件循环里）。"""
    return _get_client().embed_all(texts)