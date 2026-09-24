"""内置工具：知识库检索（risk 0）与工具结果全文取回（risk 0）。

retrieval 与 M3 的 /kb/search 调试台共用同一 Retriever —— 检索质量与
问答链路完全一致；同时把命中写入 ToolResult 供 ReAct 作为 Observation，
并由 react 层映射进 state.retrieved / state.citations。
"""

from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import Field

from agent.retrieval.search import get_retriever
from agent.tools.base import BaseTool, ToolArgs, ToolCtx, ToolMeta, ToolResult


class RetrievalArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=500, description="检索查询")
    top_k: int = Field(default=5, ge=1, le=20, description="返回父块数")


class RetrievalTool(BaseTool):
    meta = ToolMeta(
        name="retrieval",
        description=(
            "在用户的知识库中检索相关资料（向量检索）。"
            "回答知识类问题或需要引用文档内容时使用。返回带编号的资料片段。"
        ),
        args_schema=RetrievalArgs,
        risk_level=0,
        idempotent=True,
        timeout_s=10.0,
        max_result_chars=4000,
    )

    async def arun(self, ctx: ToolCtx, **kwargs: Any) -> ToolResult:
        query = str(kwargs["query"])
        top_k = int(kwargs.get("top_k", 5))
        import time

        started = time.perf_counter()
        retriever = get_retriever()
        redis = (ctx.extra or {}).get("redis")
        outcome = None
        from apps.api.core.db import tenant_session

        if redis is not None:
            # 向量预计算放在事务外（嵌入式 embedding 是网络调用，别占死连接池）；
            # 失败则交给检索层按「向量路故障」降级（§8.3），不直接中断
            try:
                qvec = await retriever.embed_query(query)
            except Exception:  # noqa: BLE001 —— embedding 挂了还有关键词路
                qvec = None
            async with tenant_session(UUID(ctx.user_id)) as session:
                outcome = await retriever.search(
                    session, UUID(ctx.user_id), query, qvec=qvec,
                    max_contexts=top_k, redis=redis,
                )
        else:
            async with tenant_session(UUID(ctx.user_id)) as session:
                outcome = await retriever.search(
                    session, UUID(ctx.user_id), query, max_contexts=top_k
                )
        hits = outcome.hits
        latency_ms = int((time.perf_counter() - started) * 1000)

        if not hits:
            note = "知识库中没有找到相关资料。可建议用户上传相关文档。"
            if outcome.degraded:
                note += f"（检索过程中有降级：{'、'.join(outcome.degraded)}）"
            return ToolResult(
                ok=True,
                content=note,
                data={"hits": [], "degraded": outcome.degraded, "diagnostics": outcome.diagnostics},
                latency_ms=latency_ms,
            )
        lines = []
        for h in hits:
            page = f"（第 {h.page} 段）" if h.page is not None else ""
            lines.append(f"[{h.rank}] 《{h.document_title}》{page}\n{h.parent_content}")
        if outcome.degraded:
            lines.append(f"（注意：本次检索有降级：{'、'.join(outcome.degraded)}）")

        # 命中结构随结果带给 react 层（映射进 state.retrieved/citations）。
        # M6 修：此前写在 error_code 里（"HITS:{json}"）而读取端读的是 data 字段
        # → ReAct 分支的 state.citations/retrieved 恒为空（M4 遗留的技术债）。
        payload = {
            "hits": [
                {
                    "chunk_id": str(h.child_chunk_id),
                    "parent_chunk_id": str(h.parent_chunk_id),
                    "document_id": str(h.document_id),
                    "document_title": h.document_title,
                    "score": h.score,
                    "rerank_score": h.rerank_score,
                    "vector_score": h.vector_score,
                    "keyword_score": h.keyword_score,
                    "rrf_score": h.rrf_score,
                    "sources": h.sources,
                    "content": h.child_content,
                    "parent_content": h.parent_content,
                    "page": h.page,
                }
                for h in hits
            ],
            "degraded": outcome.degraded,
            "diagnostics": outcome.diagnostics,
        }
        return ToolResult(
            ok=True,
            content="\n\n".join(lines),
            data=payload,
            latency_ms=latency_ms,
        )


class FetchFullArgs(ToolArgs):
    full_ref: str = Field(min_length=3, max_length=200, description="被截断结果的全局引用 id")


class FetchFullTool(BaseTool):
    """取回被截断的工具结果全文（§7.5：模型可按 full_ref 二次取用）。"""

    meta = ToolMeta(
        name="fetch_full",
        description="取回之前被截断的工具结果全文。仅在结果提示带 full_ref 时使用。",
        args_schema=FetchFullArgs,
        risk_level=0,
        idempotent=True,
        timeout_s=5.0,
        max_result_chars=20000,
    )

    def __init__(self, full_ref_dir: str = "data/tool_results") -> None:
        self._dir = Path(full_ref_dir)

    async def arun(self, ctx: ToolCtx, **kwargs: Any) -> ToolResult:
        full_ref = str(kwargs["full_ref"])
        # 路径穿越防护：只允许文件名字符，且必须落在专用目录内
        safe = Path(full_ref).name
        if safe != full_ref or ".." in full_ref or "/" in full_ref or "\\" in full_ref:
            return ToolResult(ok=False, content="非法 full_ref", error_code="VALIDATION")
        path = self._dir / safe
        if not path.exists():
            return ToolResult(ok=False, content=f"引用 {full_ref} 不存在", error_code="NOT_FOUND")
        text = path.read_text(encoding="utf-8")
        return ToolResult(ok=True, content=text)


def build_builtin_tools(full_ref_dir: str = "data/tool_results") -> list[BaseTool]:
    return [RetrievalTool(), FetchFullTool(full_ref_dir)]


def parse_hits_from_result(result: Any) -> list[dict[str, Any]]:
    """从 RetrievalTool 结果的 data 字段还原命中列表（react 层映射进 state）。"""
    data = getattr(result, "data", None) or {}
    hits = data.get("hits") if isinstance(data, dict) else None
    return hits if isinstance(hits, list) else []
