# -*- coding: utf-8 -*-
"""
api/main.py — FastAPI 后端入口
=============================
职责：
1. 创建 FastAPI 应用，注册 3 个核心接口：
   - POST /api/chat      完整多 Agent 问答流程
   - POST /api/retrieve  单独 Chroma 向量检索
   - GET  /api/health    环境健康检测（含 Chroma 数据库状态）
2. 全局异常捕获：Ollama 离线 / Chroma 文件锁 / 超时 / 校验错误；
3. 统一错误返回格式（ApiResponse + ErrorDetail）；
4. 请求超时处理（异步执行 + asyncio.wait_for）；
5. 启动时预加载 Agent 注册表（共享 Chroma，避免重复加载索引）。

依据：项目硬性规则——后端 FastAPI 按需封装，模型层全部通过 Ollama 调用，
      向量检索基于 langchain-chroma，数理计算走 RestrictedPython+SymPy 沙箱。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 项目根路径注入（保证 graph / agents / tools 模块可导入）
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.config import (
    PROJECT_ROOT,
    get_chroma_abs_path,
    get_chroma_lock_files,
    is_ollama_reachable,
    settings,
)
from api.schemas import (
    ApiResponse,
    ChatData,
    ChatRequest,
    ChatMessage,
    ErrorDetail,
    HealthData,
    KbUploadData,
    RetrieveData,
    RetrieveRequest,
    RetrievalChunk,
)


# ===========================================================================
# 一、FastAPI 应用创建
# ===========================================================================

app = FastAPI(
    title="StudyMate Agent API",
    description="多 Agent 智能学习助手后端 — 基于 LangGraph+Ollama+Chroma",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS：允许前端（Streamlit 8501）跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 学习项目，宽松配置；生产环境应限定域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================================================
# 二、统一错误响应工具
# ===========================================================================

def _error_response(
    code: str,
    message: str,
    detail: Optional[str] = None,
    status_code: int = 200,  # 统一返回 200，靠 success=False 区分，便于前端处理
    elapsed: float = 0.0,
) -> JSONResponse:
    """
    构造标准化错误响应。

    设计要点：HTTP 状态码统一 200，业务错误通过 success=False + error.code 表达，
    避免前端同时处理 HTTP 异常和业务异常。
    """
    resp = ApiResponse(
        success=False,
        data=None,
        error=ErrorDetail(code=code, message=message, detail=detail),
        elapsed=elapsed,
    )
    return JSONResponse(status_code=status_code, content=resp.model_dump())


def _ok_response(data: Any, elapsed: float = 0.0) -> JSONResponse:
    """构造标准化成功响应。"""
    resp = ApiResponse(success=True, data=data, elapsed=elapsed)
    return JSONResponse(status_code=200, content=resp.model_dump())


def _classify_exception(exc: Exception) -> Dict[str, str]:
    """
    异常分类（与 graph/workflow.classify_error 思路一致，但聚焦 HTTP 层）。

    返回 dict 包含 code / message / detail。
    """
    msg = str(exc)
    exc_type = type(exc).__name__
    detail = f"{exc_type}: {msg}"

    # 云端 API 认证失败（密钥错误、未授权）
    if settings.use_cloud_model and any(
        kw in msg.lower() for kw in ["api_key", "auth", "unauthorized", "invalid_api_key"]
    ):
        return {
            "code": "CLOUD_AUTH_FAILED",
            "message": "云端API认证失败，请检查 .env 中 CLOUD_API_KEY 是否正确配置，或切换到本地模式。",
            "detail": detail,
        }

    # 云端 API 超时或网络失败
    if settings.use_cloud_model and any(
        kw in msg.lower() for kw in ["timeout", "connection", "network", "refused"]
    ):
        return {
            "code": "CLOUD_NETWORK_ERROR",
            "message": "云端API请求超时或网络连接失败，请检查网络或切换到本地模式。",
            "detail": detail,
        }

    # 云端 API 额度耗尽
    if settings.use_cloud_model and any(
        kw in msg.lower() for kw in ["quota", "limit", "exceeded", "insufficient"]
    ):
        return {
            "code": "CLOUD_QUOTA_EXCEEDED",
            "message": "云端API额度耗尽或调用频率超限，请等待额度恢复或切换到本地模式。",
            "detail": detail,
        }

    # Ollama 离线 / 连接异常（本地模式）
    if not settings.use_cloud_model and any(
        kw in msg.lower()
        for kw in ["connection refused", "ollama", "timeout", "connect"]
    ) and "chroma" not in msg.lower():
        return {
            "code": "OLLAMA_OFFLINE",
            "message": f"Ollama 服务未启动或不可达，请先启动 Ollama 并拉取 {settings.ollama_llm_model} 模型。",
            "detail": detail,
        }

    # Chroma 文件锁冲突
    if any(kw in msg.lower() for kw in ["lock", "locked", "another process", "winerror 32"]):
        return {
            "code": "CHROMA_LOCK",
            "message": "Chroma 数据库被其他进程锁定，请关闭正在写入 Chroma 的进程后重试。",
            "detail": detail,
        }

    # Chroma 其他错误
    if "chroma" in msg.lower() or "chromadb" in msg.lower():
        return {
            "code": "CHROMA_FAILED",
            "message": "Chroma 向量库读写失败，请检查持久化目录或重启服务。",
            "detail": detail,
        }

    # 沙箱错误
    if any(kw in msg.lower() for kw in ["sandbox", "restrictedpython", "compile_restricted"]):
        return {
            "code": "SANDBOX_ERROR",
            "message": "代码沙箱执行失败，请检查表达式语法或安全限制。",
            "detail": detail,
        }

    # 超时
    if "timeout" in msg.lower() or isinstance(exc, asyncio.TimeoutError):
        return {
            "code": "TIMEOUT",
            "message": "请求处理超时，请简化问题或稍后重试。",
            "detail": detail,
        }

    # 默认内部错误
    return {
        "code": "INTERNAL",
        "message": f"服务内部错误：{exc_type}",
        "detail": detail,
    }


# ===========================================================================
# 三、全局异常处理器
# ===========================================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """处理 FastAPI HTTP 异常（如 404、422 校验错误）。"""
    return _error_response(
        code="HTTP_ERROR",
        message=exc.detail if isinstance(exc.detail, str) else "HTTP 错误",
        detail=f"status_code={exc.status_code}",
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底异常处理器，保证任何异常都返回统一格式。"""
    cls = _classify_exception(exc)
    return _error_response(
        code=cls["code"],
        message=cls["message"],
        detail=cls["detail"],
    )


# ===========================================================================
# 四、懒加载业务对象（避免 import 时即连接 Ollama/Chroma）
# ===========================================================================

_workflow_graph = None  # LangGraph 编译图（懒加载）
_vector_search = None   # ChromaVectorSearch 实例（懒加载）


def get_workflow_graph():
    """懒加载 LangGraph 工作流（首次调用时初始化，复用 registry 共享 Chroma）。"""
    global _workflow_graph
    if _workflow_graph is None:
        from graph.workflow import get_graph  # 延迟导入，避免 import 时连 Chroma
        _workflow_graph = get_graph()
    return _workflow_graph


def get_vector_search():
    """懒加载 ChromaVectorSearch 实例（与 graph.workflow.registry 共享同一 Chroma 客户端）。"""
    global _vector_search
    if _vector_search is None:
        # 优先复用 registry 已初始化的客户端，避免重复加载 542MB 索引
        from graph.workflow import registry
        registry.vector_search  # 触发懒加载
        _vector_search = registry.vector_search
    return _vector_search


# ===========================================================================
# 五、核心接口实现
# ===========================================================================

@app.get("/")
async def root():
    """根路径：返回服务信息。"""
    return {
        "service": "StudyMate Agent API",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": ["/api/chat", "/api/retrieve", "/api/health"],
    }


# ---------------------------------------------------------------------------
# 5.1 /api/chat — 多 Agent 问答
# ---------------------------------------------------------------------------

async def _run_chat_workflow(query: str, history: list) -> Dict[str, Any]:
    """
    在线程池中同步执行 graph.invoke（LangGraph 内部仍为同步调用）。
    通过 asyncio.to_thread 避免阻塞事件循环。
    """
    def _invoke():
        from graph.workflow import run_query
        return run_query(query, history)

    return await asyncio.to_thread(_invoke)


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """
    完整多 Agent 问答流程。

    流程：用户输入 → 路由 → QA/Retrieve Agent → 反思校验 → 最终答案。
    返回：final_answer / intent / retrieval_chunks / execution_log。
    """
    start = time.time()

    # 1) 前置检查：本地模式下检查 Ollama 是否在线
    if not settings.use_cloud_model and not is_ollama_reachable(timeout=2):
        return _error_response(
            code="OLLAMA_OFFLINE",
            message=f"Ollama 服务未启动，请先启动 Ollama 并拉取 {settings.ollama_llm_model} 模型。",
            detail=f"无法连接 {settings.ollama_host}",
            elapsed=time.time() - start,
        )

    # 2) 转换历史消息格式
    history = [{"role": m.role, "content": m.content} for m in req.history]

    # 3) 在超时控制下执行工作流
    try:
        timeout_sec = settings.request_timeout
        final_state = await asyncio.wait_for(
            _run_chat_workflow(req.query, history),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        return _error_response(
            code="TIMEOUT",
            message=f"工作流执行超时（>{timeout_sec}s），请简化问题或稍后重试。",
            elapsed=time.time() - start,
        )
    except Exception as exc:
        cls = _classify_exception(exc)
        return _error_response(
            code=cls["code"],
            message=cls["message"],
            detail=cls["detail"],
            elapsed=time.time() - start,
        )

    # 4) 组装响应数据
    elapsed = time.time() - start
    chunks = [
        RetrievalChunk(
            content=c.get("content", ""),
            score=float(c.get("score", 0.0)),
            metadata=c.get("metadata", {}),
        )
        for c in final_state.get("retrieval_chunks", [])
    ]

    data = ChatData(
        final_answer=final_state.get("final_answer", ""),
        intent=final_state.get("intent", ""),
        retrieval_chunks=chunks,
        execution_log=final_state.get("execution_log", []),
        reflection_output=final_state.get("reflection_output", ""),
        model_mode=final_state.get("model_mode", "local"),
        elapsed=elapsed,
    )
    return _ok_response(data, elapsed=elapsed)


# ---------------------------------------------------------------------------
# 5.2 /api/retrieve — 单独向量检索
# ---------------------------------------------------------------------------

async def _run_retrieve(query: str, k: int, filter_: Optional[Dict], score_threshold: Optional[float]):
    """在线程池中执行同步检索。"""
    def _do():
        vs = get_vector_search()
        return vs.search(query=query, k=k, filter=filter_, score_threshold=score_threshold)
    return await asyncio.to_thread(_do)


@app.post("/api/retrieve")
async def retrieve(req: RetrieveRequest):
    """单独 Chroma 向量检索，返回召回片段 + 集合统计。"""
    start = time.time()

    try:
        timeout_sec = settings.retrieve_timeout
        results = await asyncio.wait_for(
            _run_retrieve(req.query, req.k, req.filter, req.score_threshold),
            timeout=timeout_sec,
        )
        # 同时取集合统计（非阻塞失败容忍）
        try:
            stats = await asyncio.to_thread(lambda: get_vector_search().get_collection_stats())
        except Exception:
            stats = {}
    except asyncio.TimeoutError:
        return _error_response(
            code="TIMEOUT",
            message=f"向量检索超时（>{settings.retrieve_timeout}s）。",
            elapsed=time.time() - start,
        )
    except Exception as exc:
        cls = _classify_exception(exc)
        return _error_response(
            code=cls["code"],
            message=cls["message"],
            detail=cls["detail"],
            elapsed=time.time() - start,
        )

    chunks = [
        RetrievalChunk(
            content=r.get("content", ""),
            score=float(r.get("score", 0.0)),
            metadata=r.get("metadata", {}),
        )
        for r in results
    ]
    data = RetrieveData(
        query=req.query,
        total=len(chunks),
        chunks=chunks,
        collection_stats=stats,
    )
    return _ok_response(data, elapsed=time.time() - start)


# ---------------------------------------------------------------------------
# 5.3 /api/health — 环境健康检测
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    """
    环境健康检测：
    - 模型模式（本地/Ollama 或云端/API）；
    - Ollama/云端连通性 + 模型可用性；
    - Chroma 目录存在性 + 可打开 + 锁文件数；
    - 关键配置摘要。
    """
    start = time.time()
    checks: List[Dict[str, Any]] = []

    # ---- 模型模式 ----
    model_mode = "cloud" if settings.use_cloud_model else "local"
    checks.append({
        "name": "模型模式",
        "passed": True,
        "message": f"当前模式: {'云端API模型' if settings.use_cloud_model else '本地Ollama模型'}",
    })

    # ---- 云端模式检测 ----
    if settings.use_cloud_model:
        cloud_api_key_set = bool(settings.cloud_api_key and settings.cloud_api_key != "your-cloud-api-key")
        cloud_llm_url_set = bool(settings.cloud_llm_base_url and settings.cloud_llm_base_url != "https://api.example.com/v1")
        
        checks.append({
            "name": "云端API密钥",
            "passed": cloud_api_key_set,
            "message": "已配置" if cloud_api_key_set else "未配置或使用默认值",
        })
        checks.append({
            "name": "云端LLM地址",
            "passed": cloud_llm_url_set,
            "message": f"{settings.cloud_llm_base_url}" if cloud_llm_url_set else "未配置或使用默认值",
        })
        checks.append({
            "name": "云端LLM模型",
            "passed": bool(settings.cloud_llm_model_name),
            "message": settings.cloud_llm_model_name or "未配置",
        })
        checks.append({
            "name": "云端嵌入模型",
            "passed": bool(settings.cloud_embed_model_name),
            "message": settings.cloud_embed_model_name or "未配置",
        })

        # 尝试检测云端API连通性
        cloud_reachable = False
        if cloud_api_key_set and cloud_llm_url_set:
            try:
                import urllib.request
                url = f"{settings.cloud_llm_base_url.rstrip('/')}/models"
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {settings.cloud_api_key}"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    cloud_reachable = resp.status == 200
            except Exception:
                cloud_reachable = False
        
        checks.append({
            "name": "云端API连通性",
            "passed": cloud_reachable,
            "message": "连通" if cloud_reachable else "无法连通，请检查网络或密钥",
        })
    else:
        # ---- 本地模式 Ollama 检测 ----
        ollama_ok = is_ollama_reachable(timeout=3)
        ollama_models: List[str] = []
        if ollama_ok:
            try:
                import json
                import urllib.request
                host = settings.ollama_host.strip()
                if not host.startswith(("http://", "https://")):
                    host = f"http://{host}"
                url = f"{host.rstrip('/')}/api/tags"
                with urllib.request.urlopen(url, timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    ollama_models = [m["name"] for m in data.get("models", [])]
            except Exception:
                ollama_models = []

        checks.append({
            "name": "Ollama 连通性",
            "passed": ollama_ok,
            "message": f"Ollama {'在线' if ollama_ok else '离线'} @ {settings.ollama_host}",
        })

        required_models = [settings.ollama_llm_model, settings.ollama_embed_model]
        for model in required_models:
            present = model in ollama_models
            checks.append({
                "name": f"模型 {model}",
                "passed": present,
                "message": "已拉取" if present else f"未拉取，请执行 ollama pull {model}",
            })

    # ---- Chroma 检测 ----
    chroma_path = get_chroma_abs_path()
    chroma_exists = chroma_path.exists()
    checks.append({
        "name": "Chroma 持久化目录",
        "passed": chroma_exists,
        "message": f"{chroma_path} {'存在' if chroma_exists else '不存在'}",
    })

    chroma_openable = False
    chroma_doc_count = 0
    chroma_error = None
    if chroma_exists:
        try:
            def _probe():
                vs = get_vector_search()
                stats = vs.get_collection_stats()
                return stats
            stats = await asyncio.to_thread(_probe)
            chroma_openable = True
            chroma_doc_count = stats.get("count", 0) if isinstance(stats, dict) else 0
        except Exception as exc:
            chroma_error = f"{type(exc).__name__}: {exc}"

    checks.append({
        "name": "Chroma 数据库可打开",
        "passed": chroma_openable,
        "message": (
            f"集合可读，文档数≈{chroma_doc_count}"
            if chroma_openable
            else f"打开失败：{chroma_error}"
        ),
    })

    lock_files = get_chroma_lock_files()
    checks.append({
        "name": "Chroma 锁文件",
        "passed": len(lock_files) == 0,
        "message": (
            "无锁文件"
            if not lock_files
            else f"检测到 {len(lock_files)} 个 .lock 文件（可能有进程占用）"
        ),
    })

    # ---- 整体状态 ----
    all_passed = all(c["passed"] for c in checks)
    if all_passed:
        status = "healthy"
    else:
        status = "degraded"

    data = HealthData(
        status=status,
        model_mode=model_mode,
        ollama={
            "host": settings.ollama_host,
            "reachable": is_ollama_reachable(timeout=3) if not settings.use_cloud_model else False,
            "models": [],
            "required": [],
        },
        cloud={
            "api_key_configured": bool(settings.cloud_api_key and settings.cloud_api_key != "your-cloud-api-key"),
            "llm_base_url": settings.cloud_llm_base_url,
            "llm_model_name": settings.cloud_llm_model_name,
            "embed_model_name": settings.cloud_embed_model_name,
        },
        chroma={
            "persist_directory": str(chroma_path),
            "exists": chroma_exists,
            "openable": chroma_openable,
            "doc_count": chroma_doc_count,
            "lock_files": lock_files,
            "collection_name": settings.chroma_collection_name,
            "error": chroma_error,
        },
        config={
            "llm_model": settings.cloud_llm_model_name if settings.use_cloud_model else settings.ollama_llm_model,
            "embed_model": settings.cloud_embed_model_name if settings.use_cloud_model else settings.ollama_embed_model,
            "fastapi_host": settings.fastapi_host,
            "fastapi_port": settings.fastapi_port,
            "streamlit_port": settings.streamlit_port,
            "app_mode": settings.app_mode,
            "use_cloud_model": settings.use_cloud_model,
        },
        checks=checks,
    )
    return _ok_response(data, elapsed=time.time() - start)


# ---------------------------------------------------------------------------
# 5.4 /api/kb/upload — 知识库文件上传入库（可选接口，供前端调用）
# ---------------------------------------------------------------------------

@app.post("/api/kb/upload")
async def kb_upload(files: List[UploadFile] = File(...)):
    """
    批量上传 PDF/docx，自动解析+分块+入库 Chroma。
    前端"知识库上传页"在前后端分离模式下调用此接口。
    """
    start = time.time()

    if not files:
        return _error_response(
            code="VALIDATION",
            message="未上传任何文件。",
            elapsed=0.0,
        )

    # 保存到临时目录
    upload_dir = PROJECT_ROOT / "datasets" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: List[Path] = []
    skipped: List[str] = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in [".pdf", ".docx"]:
            skipped.append(f.filename or "未知文件")
            continue
        save_path = upload_dir / (f.filename or "uploaded")
        try:
            content = await f.read()
            with open(save_path, "wb") as fp:
                fp.write(content)
            saved_paths.append(save_path)
        except Exception:
            skipped.append(f.filename or "未知文件")

    if not saved_paths:
        return _error_response(
            code="VALIDATION",
            message="没有可处理的 PDF/docx 文件。",
            detail=f"跳过：{skipped}",
            elapsed=time.time() - start,
        )

    # 调用批量入库逻辑（在线程池中执行，避免阻塞）
    def _build():
        from data_process.batch_build_kb import BatchKBBuilder
        
        # 云端模式下不传递本地嵌入模型名，让工厂函数自动选择
        embedding_model = None if settings.use_cloud_model else settings.ollama_embed_model
        
        builder = BatchKBBuilder(
            persist_directory=settings.chroma_persist_directory,
            collection_name=settings.chroma_collection_name,
            batch_size=50,
            embedding_model=embedding_model,
        )
        added = builder.build_from_documents(str(upload_dir), strategy="lecture")
        try:
            stats = builder.vector_search.get_collection_stats()
        except Exception:
            stats = {}
        return added, stats

    try:
        added_count, stats = await asyncio.to_thread(_build)
    except Exception as exc:
        cls = _classify_exception(exc)
        return _error_response(
            code=cls["code"],
            message=cls["message"],
            detail=cls["detail"],
            elapsed=time.time() - start,
        )

    data = KbUploadData(
        total_files=len(files),
        parsed_files=len(saved_paths),
        total_chunks=added_count,
        added_count=added_count,
        skipped=skipped,
        collection_stats=stats,
    )
    return _ok_response(data, elapsed=time.time() - start)


# ===========================================================================
# 六、启动事件：打印启动信息
# ===========================================================================

@app.on_event("startup")
async def startup_event():
    """启动时打印关键配置（不预加载 Chroma，避免冷启动慢）。"""
    print("=" * 60)
    print("  StudyMate Agent API — 启动中")
    print("=" * 60)
    if settings.use_cloud_model:
        print(f"  模型模式  : 云端API")
        print(f"  LLM URL  : {settings.cloud_llm_base_url}")
        print(f"  LLM Model: {settings.cloud_llm_model_name}")
        print(f"  Embed    : {settings.cloud_embed_model_name}")
    else:
        print(f"  模型模式  : 本地Ollama")
        print(f"  Ollama   : {settings.ollama_host}")
        print(f"  LLM Model: {settings.ollama_llm_model}")
        print(f"  Embed    : {settings.ollama_embed_model}")
    print(f"  Chroma   : {get_chroma_abs_path()}")
    print(f"  Listen   : {settings.fastapi_host}:{settings.fastapi_port}")
    print(f"  Docs     : http://localhost:{settings.fastapi_port}/docs")
    print("=" * 60)
    print("  注：Agent 与 Chroma 客户端将在首次请求时懒加载。")
    print("=" * 60)


# ===========================================================================
# 七、直接运行入口：uvicorn api.main:app
# ===========================================================================

def main() -> None:
    """直接 python api/main.py 启动（便于调试，推荐生产用 uvicorn 命令）。"""
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=settings.fastapi_host,
        port=settings.fastapi_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
