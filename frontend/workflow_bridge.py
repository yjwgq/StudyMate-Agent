# -*- coding: utf-8 -*-
"""
frontend/workflow_bridge.py — 一体化模式桥接层
=============================================
职责：
在"一体化模式"下，Streamlit 直接调用 graph 工作流与 tools 工具，
无需启动 FastAPI 后端。

设计要点：
1. 接口签名与 ApiClient 完全一致（chat/retrieve/health/upload_kb），
   页面层只需切换 backend 即可，无需改动业务逻辑；
2. 错误返回结构与分离模式统一（{success, data, error, elapsed}）；
3. 懒加载：首次调用时才导入 graph/tools，避免 Streamlit 启动时即连 Chroma；
4. 异常分类与 api/main.py 的 _classify_exception 一致，便于前端友好弹窗。

依据：项目硬性规则——前端可一体化运行，模型层全部通过 Ollama 调用，
      向量检索基于 langchain-chroma，数理计算走 RestrictedPython+SymPy 沙箱。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 项目根路径注入（保证 graph / agents / tools / api 模块可导入）
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from api.config import (
    get_chroma_abs_path,
    get_chroma_lock_files,
    is_ollama_reachable,
    settings,
)


# ---------------------------------------------------------------------------
# 异常分类（与 api/main.py 一致）
# ---------------------------------------------------------------------------
def _classify_exception(exc: Exception) -> Dict[str, str]:
    msg = str(exc)
    exc_type = type(exc).__name__
    detail = f"{exc_type}: {msg}"

    if any(kw in msg.lower() for kw in ["connection refused", "ollama", "timeout", "connect"]) \
            and "chroma" not in msg.lower():
        return {
            "code": "OLLAMA_OFFLINE",
            "message": "Ollama 服务未启动或不可达，请先启动 Ollama 并拉取对应模型（参见 .env 配置）。",
            "detail": detail,
        }
    if any(kw in msg.lower() for kw in ["lock", "locked", "another process", "winerror 32"]):
        return {
            "code": "CHROMA_LOCK",
            "message": "Chroma 数据库被其他进程锁定，请关闭正在写入 Chroma 的进程后重试。",
            "detail": detail,
        }
    if "chroma" in msg.lower() or "chromadb" in msg.lower():
        return {
            "code": "CHROMA_FAILED",
            "message": "Chroma 向量库读写失败，请检查持久化目录或重启服务。",
            "detail": detail,
        }
    return {
        "code": "INTERNAL",
        "message": f"服务内部错误：{exc_type}",
        "detail": detail,
    }


def _ok(data: Any, elapsed: float = 0.0) -> Dict[str, Any]:
    """构造成功响应（与 FastAPI 返回结构一致）。"""
    return {"success": True, "data": data, "error": None, "elapsed": elapsed}


def _fail(exc: Exception, elapsed: float = 0.0) -> Dict[str, Any]:
    """构造失败响应。"""
    cls = _classify_exception(exc)
    return {
        "success": False,
        "data": None,
        "error": {
            "code": cls["code"],
            "message": cls["message"],
            "detail": cls["detail"],
        },
        "elapsed": elapsed,
    }


# ===========================================================================
# 一体化桥接主体
# ===========================================================================

class WorkflowBridge:
    """
    一体化模式桥接：直接调用 graph 工作流。

    与 ApiClient 接口对齐，页面层可透明切换。
    """

    def __init__(self) -> None:
        self._vector_search = None  # 懒加载

    # ------------------------------------------------------------------
    # 懒加载工具
    # ------------------------------------------------------------------
    def _get_vector_search(self):
        """复用 graph.workflow.registry 的共享 Chroma 客户端。"""
        if self._vector_search is None:
            from graph.workflow import registry
            self._vector_search = registry.vector_search  # 触发懒加载
        return self._vector_search

    # ------------------------------------------------------------------
    # chat：完整多 Agent 问答
    # ------------------------------------------------------------------
    def chat(
        self,
        query: str,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: float = 0.7,
    ) -> Dict[str, Any]:
        """
        直接调用 graph.workflow.run_query。

        与 /api/chat 返回结构一致：
        data = {final_answer, intent, retrieval_chunks, execution_log, reflection_output, elapsed}
        """
        start = time.time()

        # 1) Ollama 前置检测
        if not is_ollama_reachable(timeout=2):
            return {
                "success": False,
                "data": None,
                "error": {
                    "code": "OLLAMA_OFFLINE",
                    "message": f"Ollama 服务未启动，请先启动 Ollama 并拉取 {settings.ollama_llm_model} 模型。",
                    "detail": f"无法连接 {settings.ollama_host}",
                },
                "elapsed": time.time() - start,
            }

        # 2) 调用工作流
        try:
            from graph.workflow import run_query
            final_state = run_query(query, history or [])
        except Exception as exc:
            return _fail(exc, elapsed=time.time() - start)

        # 3) 组装响应（与 api/main.py 一致）
        chunks = [
            {
                "content": c.get("content", ""),
                "score": float(c.get("score", 0.0)),
                "metadata": c.get("metadata", {}),
            }
            for c in final_state.get("retrieval_chunks", [])
        ]
        data = {
            "final_answer": final_state.get("final_answer", ""),
            "intent": final_state.get("intent", ""),
            "retrieval_chunks": chunks,
            "execution_log": final_state.get("execution_log", []),
            "reflection_output": final_state.get("reflection_output", ""),
            "elapsed": time.time() - start,
        }
        return _ok(data, elapsed=time.time() - start)

    # ------------------------------------------------------------------
    # retrieve：单独向量检索
    # ------------------------------------------------------------------
    def retrieve(
        self,
        query: str,
        k: int = 5,
        filter_: Optional[Dict[str, Any]] = None,
        score_threshold: Optional[float] = None,
    ) -> Dict[str, Any]:
        """直接调用 ChromaVectorSearch.search。"""
        start = time.time()
        try:
            vs = self._get_vector_search()
            results = vs.search(
                query=query, k=k, filter=filter_, score_threshold=score_threshold
            )
            try:
                stats = vs.get_collection_stats()
            except Exception:
                stats = {}
        except Exception as exc:
            return _fail(exc, elapsed=time.time() - start)

        chunks = [
            {
                "content": r.get("content", ""),
                "score": float(r.get("score", 0.0)),
                "metadata": r.get("metadata", {}),
            }
            for r in results
        ]
        data = {
            "query": query,
            "total": len(chunks),
            "chunks": chunks,
            "collection_stats": stats,
        }
        return _ok(data, elapsed=time.time() - start)

    # ------------------------------------------------------------------
    # health：健康检测
    # ------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        """本地健康检测（与 /api/health 结构一致）。"""
        start = time.time()
        checks: List[Dict[str, Any]] = []

        # Ollama
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

        # Chroma
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
                vs = self._get_vector_search()
                stats = vs.get_collection_stats()
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

        # 整体状态
        ollama_passed = ollama_ok and all(m in ollama_models for m in required_models)
        chroma_passed = chroma_openable
        if ollama_passed and chroma_passed:
            status = "healthy"
        elif ollama_passed or chroma_passed:
            status = "degraded"
        else:
            status = "unhealthy"

        data = {
            "status": status,
            "ollama": {
                "host": settings.ollama_host,
                "reachable": ollama_ok,
                "models": ollama_models,
                "required": required_models,
            },
            "chroma": {
                "persist_directory": str(chroma_path),
                "exists": chroma_exists,
                "openable": chroma_openable,
                "doc_count": chroma_doc_count,
                "lock_files": lock_files,
                "collection_name": settings.chroma_collection_name,
                "error": chroma_error,
            },
            "config": {
                "llm_model": settings.ollama_llm_model,
                "embed_model": settings.ollama_embed_model,
                "fastapi_host": settings.fastapi_host,
                "fastapi_port": settings.fastapi_port,
                "streamlit_port": settings.streamlit_port,
                "app_mode": settings.app_mode,
            },
            "checks": checks,
        }
        return _ok(data, elapsed=time.time() - start)

    # ------------------------------------------------------------------
    # upload_kb：知识库上传入库
    # ------------------------------------------------------------------
    def upload_kb(self, files: List[Any]) -> Dict[str, Any]:
        """
        一体化模式：直接保存文件到 datasets/uploads，调用 BatchKBBuilder 入库。

        Args:
            files: Streamlit UploadedFile 列表
        """
        start = time.time()

        if not files:
            return {
                "success": False,
                "data": None,
                "error": {
                    "code": "VALIDATION",
                    "message": "未上传任何文件。",
                    "detail": None,
                },
                "elapsed": 0.0,
            }

        upload_dir = Path(_PROJECT_ROOT) / "datasets" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)

        saved_paths: List[Path] = []
        skipped: List[str] = []
        for f in files:
            name = f.name if hasattr(f, "name") else "uploaded"
            ext = Path(name).suffix.lower()
            if ext not in [".pdf", ".docx"]:
                skipped.append(name)
                continue
            save_path = upload_dir / name
            try:
                content = f.read() if hasattr(f, "read") else f[1]
                with open(save_path, "wb") as fp:
                    fp.write(content)
                saved_paths.append(save_path)
            except Exception:
                skipped.append(name)

        if not saved_paths:
            return {
                "success": False,
                "data": None,
                "error": {
                    "code": "VALIDATION",
                    "message": "没有可处理的 PDF/docx 文件。",
                    "detail": f"跳过：{skipped}",
                },
                "elapsed": time.time() - start,
            }

        try:
            from data_process.batch_build_kb import BatchKBBuilder
            builder = BatchKBBuilder(
                persist_directory=settings.chroma_persist_directory,
                collection_name=settings.chroma_collection_name,
                batch_size=50,
                embedding_model=settings.ollama_embed_model,
            )
            added = builder.build_from_documents(str(upload_dir), strategy="lecture")
            try:
                stats = builder.vector_search.get_collection_stats()
            except Exception:
                stats = {}
        except Exception as exc:
            return _fail(exc, elapsed=time.time() - start)

        data = {
            "total_files": len(files),
            "parsed_files": len(saved_paths),
            "total_chunks": added,
            "added_count": added,
            "skipped": skipped,
            "collection_stats": stats,
        }
        return _ok(data, elapsed=time.time() - start)

    # ------------------------------------------------------------------
    # ping：一体化模式默认"在线"
    # ------------------------------------------------------------------
    def ping(self) -> bool:
        """一体化模式无需后端，始终返回 True。"""
        return True


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
workflow_bridge = WorkflowBridge()


# ---------------------------------------------------------------------------
# 统一 Backend 选择器：根据 app_mode 自动返回对应后端
# ---------------------------------------------------------------------------
def get_backend(mode: Optional[str] = None):
    """
    根据运行模式返回对应后端实例。

    Args:
        mode: "integrated" / "separated"，默认从 .env 读取 APP_MODE

    Returns:
        WorkflowBridge 或 ApiClient 实例（接口签名一致）
    """
    mode = mode or os.getenv("APP_MODE", "separated").lower()
    if mode == "integrated":
        return workflow_bridge
    return __import__("frontend.api_client", fromlist=["api_client"]).api_client


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------
def main() -> None:
    """workflow_bridge.py 自测：调用健康检测。"""
    print("[WorkflowBridge] 一体化模式自测")
    result = workflow_bridge.health()
    print(result)


if __name__ == "__main__":
    main()
