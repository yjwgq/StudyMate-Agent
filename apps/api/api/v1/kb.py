"""知识库接口（M2-1/2 + M3 检索调试台，§8.1 + §13.7 + §8.2）。

POST /api/v1/kb/documents        上传（校验 → 去重 → 落库 processing → 投递 ingest）
GET  /api/v1/kb/documents        列表
GET  /api/v1/kb/documents/{id}   状态查询（C1 验收：processing → ready）
POST /api/v1/kb/documents/{id}/reingest   手动重跑 ingest（C5 验收）
POST /api/v1/kb/search           检索调试台（M3-8）：单路检索 → 命中+分数
                                 （D5 验收也用它做租户隔离的自动化断言）

设计要点：
    - 大小限制用流式累计，不信任 Content-Length（可伪造）；
    - magic bytes 以文件真实内容判定类型（C7：改后缀名的文件被拦）；
    - content_hash = sha256(文件字节)，UNIQUE(user_id, content_hash) 数据库
      兜底 —— 同用户同文件重复上传直接返回已有文档（C4）；
    - 文件名以 document_id 命名落盘，原始文件名净化后仅作展示
      （路径穿越 / 特殊字符攻击面直接消失，§13.7）；
    - ingest 是异步任务：接口只负责「收文件 + 建记录 + 投递」，
      返回 202，状态经 GET 轮询（M2 不做前端进度推送）。
"""

import hashlib
import logging
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, Field

from agent.retrieval.search import get_retriever
from apps.api.core.config import settings
from apps.api.core.db import tenant_session
from apps.api.core.deps import UserCtx, current_user
from apps.api.core.errors import AppError, ErrorCode
from apps.api.core.redis import get_redis
from apps.api.core.upload_guard import (
    UploadRejected,
    check_suffix,
    kind_from_suffix,
    sanitize_filename,
    sniff_kind,
)
from apps.api.repositories import kb as kb_repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kb", tags=["kb"])

_READ_CHUNK = 1024 * 1024  # 1MB


def _send_ingest_task(document_id: str) -> None:
    """投递 ingest 任务。按名字发送，API 不 import worker 的任务实现。"""
    from worker.celery_app import celery_app

    celery_app.send_task(
        "worker.tasks.ingest.ingest_document", args=[document_id], queue="ingest"
    )


@router.post("/documents", status_code=202)
async def upload_document(
    request: Request,
    user: Annotated[UserCtx, Depends(current_user)],
    file: Annotated[UploadFile, File(description="PDF / DOCX / MD / TXT / HTML")],
) -> dict:
    trace_id = getattr(request.state, "trace_id", "")

    # ---- 1) 大小限制：流式累计（§13.7，C6）----
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.upload_max_bytes:
            raise AppError(
                ErrorCode.UPLOAD_REJECTED,
                f"文件超过大小上限 {settings.upload_max_mb}MB",
                413,
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise AppError(ErrorCode.UPLOAD_REJECTED, "空文件", 413)

    # ---- 2) 类型校验：后缀白名单 + magic bytes + 一致性（§13.7，C7）----
    original_name = file.filename or ""
    try:
        check_suffix(original_name)
        kind = sniff_kind(data)
        # 声明（后缀）与内容（magic）必须一致：.pdf 后缀装文本的改名文件在此拦下
        expected = kind_from_suffix(original_name)
        if expected is not None and expected != kind:
            raise UploadRejected(
                "文件内容与扩展名不符（疑似改名文件）", reason="fake_extension"
            )
    except UploadRejected as exc:
        raise AppError(
            ErrorCode.UPLOAD_REJECTED, exc.message, 415, detail={"reason": exc.reason}
        ) from exc

    # ---- 3) content_hash 去重 + 配额 + 建记录（事务内，UNIQUE 兜底）----
    content_hash = hashlib.sha256(data).hexdigest()
    safe_name = sanitize_filename(original_name)
    async with tenant_session(user.id) as session:
        existing = await kb_repo.get_document_by_hash(session, user.id, content_hash)
        if existing is not None:
            return {
                "data": {"duplicated": True, "document": _doc_view(existing)},
                "trace_id": trace_id,
            }
        count = await kb_repo.count_documents(session, user.id)
        if count >= settings.doc_quota_per_user:
            raise AppError(
                ErrorCode.QUOTA_EXCEEDED,
                f"文档数量达到配额上限 {settings.doc_quota_per_user}",
                429,
            )
        doc = await kb_repo.create_document(
            session,
            user_id=user.id,
            title=safe_name,
            content_hash=content_hash,
            meta={"filename": safe_name, "size": total, "kind": kind,
                  "mime": file.content_type or ""},
        )

    # ---- 4) 落盘（存储名 = document_id，与原始文件名解耦）----
    doc_dir = Path(settings.upload_dir) / str(user.id)
    suffix = safe_name[safe_name.rfind("."):] if "." in safe_name else ".bin"
    doc_path = doc_dir / f"{doc['id']}{suffix}"
    try:
        doc_dir.mkdir(parents=True, exist_ok=True)
        doc_path.write_bytes(data)
    except OSError as exc:
        # 落盘失败：不留 processing 僵尸记录
        async with tenant_session(user.id) as session:
            await kb_repo.delete_document(session, user.id, doc["id"])
        raise AppError(
            ErrorCode.INTERNAL, "文件保存失败，请稍后重试", 500
        ) from exc

    # ---- 5) 投递异步 ingest ----
    try:
        _send_ingest_task(str(doc["id"]))
    except Exception as exc:  # noqa: BLE001 —— broker 不可用：明确失败而非假成功
        logger.error("ingest task dispatch failed doc=%s: %s", doc["id"], exc)
        raise AppError(
            ErrorCode.INTERNAL, "任务投递失败，请稍后重试", 500
        ) from exc

    return {
        "data": {"duplicated": False, "document": _doc_view({**doc, "status": "processing"})},
        "trace_id": trace_id,
    }


def _doc_view(doc: dict) -> dict:
    return {
        "id": str(doc["id"]),
        "title": doc.get("title"),
        "status": doc.get("status"),
        "chunk_count": doc.get("chunk_count"),
        "parser_used": doc.get("parser_used"),
        "error_message": doc.get("error_message"),
        "created_at": str(doc.get("created_at", "")),
    }


@router.get("/documents")
async def list_documents(
    user: Annotated[UserCtx, Depends(current_user)],
) -> dict:
    async with tenant_session(user.id) as session:
        docs = await kb_repo.list_documents(session, user.id)
    return {"data": {"items": [_doc_view(d) for d in docs]}}


@router.get("/documents/{document_id}")
async def get_document(
    document_id: UUID,
    user: Annotated[UserCtx, Depends(current_user)],
) -> dict:
    async with tenant_session(user.id) as session:
        doc = await kb_repo.get_document(session, user.id, document_id)
    if doc is None:
        # 与 B4 同一设计：不区分「不存在」与「不属于你」
        raise AppError(ErrorCode.NOT_FOUND, "文档不存在", 404)
    return {"data": {"document": _doc_view(doc)}}


@router.post("/documents/{document_id}/reingest")
async def reingest_document(
    document_id: UUID,
    request: Request,
    user: Annotated[UserCtx, Depends(current_user)],
) -> dict:
    """手动重跑 ingest（C5）。失败重试 / 断点续传验证入口。"""
    trace_id = getattr(request.state, "trace_id", "")
    async with tenant_session(user.id) as session:
        doc = await kb_repo.get_document(session, user.id, document_id)
        if doc is None:
            raise AppError(ErrorCode.NOT_FOUND, "文档不存在", 404)
        await kb_repo.reset_document(session, document_id)
    _send_ingest_task(str(document_id))
    return {"data": {"ok": True, "document_id": str(document_id)}, "trace_id": trace_id}


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000, description="检索查询")
    top_k: int = Field(default=6, ge=1, le=50, description="返回父块数上限")


@router.post("/search")
async def search_kb(
    req: SearchRequest,
    user: Annotated[UserCtx, Depends(current_user)],
) -> dict:
    """检索调试台（M3-8，§8.2）：单路向量检索，返回命中父块与分数。

    与 chat 共用同一 Retriever —— 调试台看到的排序就是问答时注入的顺序。
    检索失败返回 503 SEARCH_FAILED（embedding / DB 异常）。
    """
    retriever = get_retriever()
    try:
        qvec = await retriever.embed_query(req.query)
        async with tenant_session(user.id) as session:
            outcome = await retriever.search(
                session, user.id, req.query, qvec=qvec,
                top_k=req.top_k * 3, max_contexts=req.top_k, redis=get_redis(),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb/search 失败 uid=%s: %s", user.id, exc)
        raise AppError(ErrorCode.SEARCH_FAILED, "检索服务暂不可用", 503) from exc
    return {
        "data": {
            "query": req.query,
            # 逐路对比视图（M6）：每命中带各路分数与来源，便于判断
            # 「混合检索/RRF/精排各自贡献了什么」，也是降级排障的第一现场
            "hits": [
                {
                    "n": h.rank,
                    "score": h.score,
                    "rerank_score": h.rerank_score,
                    "vector_score": h.vector_score,
                    "keyword_score": h.keyword_score,
                    "rrf_score": h.rrf_score,
                    "sources": h.sources,
                    "document_id": str(h.document_id) if h.document_id else None,
                    "title": h.document_title,
                    "page": h.page,
                    "snippet": h.child_content[:200],
                    "parent_content": h.parent_content,
                }
                for h in outcome.hits
            ],
            "degraded": outcome.degraded,
            "diagnostics": outcome.diagnostics,
            "elapsed_ms": outcome.elapsed_ms,
        }
    }
