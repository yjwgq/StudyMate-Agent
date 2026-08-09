# -*- coding: utf-8 -*-
"""
frontend/api_client.py — 前后端分离模式客户端
=============================================
职责：
封装对 FastAPI 后端的 HTTP 调用，提供与 workflow_bridge 完全一致的接口签名，
使页面层无需关心当前是分离模式还是一体化模式。

核心方法（与 WorkflowBridge 对齐）：
- chat(query, history, temperature)        → /api/chat
- retrieve(query, k, filter_, score_th)    → /api/retrieve
- health()                                  → /api/health
- upload_kb(files)                          → /api/kb/upload

依据：项目硬性规则——前端 requests 调用 FastAPI 接口；
      统一错误返回格式（success/data/error）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# 项目根路径注入（保证可读取 .env）
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in os.sys.path:
    os.sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv(dotenv_path=str(Path(_PROJECT_ROOT) / ".env"))


# ---------------------------------------------------------------------------
# 配置：FastAPI 后端地址
# ---------------------------------------------------------------------------
DEFAULT_HOST = os.getenv("FASTAPI_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("FASTAPI_PORT", "8000"))

# FastAPI 监听 0.0.0.0 时，客户端应使用 127.0.0.1 访问
if DEFAULT_HOST in ("0.0.0.0", "::"):
    DEFAULT_HOST = "127.0.0.1"

API_BASE_URL = os.getenv("API_BASE_URL", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")


class ApiClient:
    """
    FastAPI 后端 HTTP 客户端。

    所有方法返回与 WorkflowBridge 一致的 dict 结构，便于上层透明切换。
    错误统一封装为 {"success": False, "error": {"code":..., "message":..., "detail":...}}。
    """

    def __init__(self, base_url: str = None, timeout: int = 180) -> None:
        self.base_url = (base_url or API_BASE_URL).rstrip("/")
        self.timeout = timeout  # 多 Agent 流程较慢，默认 180s

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _post(self, path: str, json_body: Dict[str, Any]) -> Dict[str, Any]:
        """统一 POST 请求，自动处理 HTTP 异常与业务错误。"""
        url = f"{self.base_url}{path}"
        try:
            resp = requests.post(url, json=json_body, timeout=self.timeout)
        except requests.exceptions.ConnectionError:
            return {
                "success": False,
                "error": {
                    "code": "BACKEND_OFFLINE",
                    "message": "无法连接后端服务，请确认 FastAPI 已启动（默认端口 8000）。",
                    "detail": f"URL={url}",
                },
            }
        except requests.exceptions.Timeout:
            return {
                "success": False,
                "error": {
                    "code": "TIMEOUT",
                    "message": "后端响应超时，请稍后重试。",
                    "detail": f"timeout={self.timeout}s",
                },
            }
        except Exception as exc:
            return {
                "success": False,
                "error": {
                    "code": "CLIENT_ERROR",
                    "message": f"客户端异常：{type(exc).__name__}",
                    "detail": str(exc),
                },
            }

        try:
            payload = resp.json()
        except ValueError:
            return {
                "success": False,
                "error": {
                    "code": "INVALID_RESPONSE",
                    "message": "后端返回非 JSON 数据，请检查服务状态。",
                    "detail": f"HTTP {resp.status_code}: {resp.text[:200]}",
                },
            }

        # FastAPI 统一返回 {success, data, error, elapsed} 结构
        return payload

    def _get(self, path: str) -> Dict[str, Any]:
        """统一 GET 请求。"""
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(url, timeout=self.timeout)
        except requests.exceptions.ConnectionError:
            return {
                "success": False,
                "error": {
                    "code": "BACKEND_OFFLINE",
                    "message": "无法连接后端服务，请确认 FastAPI 已启动。",
                    "detail": f"URL={url}",
                },
            }
        except Exception as exc:
            return {
                "success": False,
                "error": {
                    "code": "CLIENT_ERROR",
                    "message": f"客户端异常：{type(exc).__name__}",
                    "detail": str(exc),
                },
            }
        try:
            return resp.json()
        except ValueError:
            return {
                "success": False,
                "error": {
                    "code": "INVALID_RESPONSE",
                    "message": "后端返回非 JSON 数据。",
                    "detail": f"HTTP {resp.status_code}",
                },
            }

    # ------------------------------------------------------------------
    # 4 个核心接口封装
    # ------------------------------------------------------------------
    def chat(
        self,
        query: str,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: float = 0.7,
    ) -> Dict[str, Any]:
        """/api/chat — 多 Agent 问答。"""
        body = {
            "query": query,
            "history": history or [],
            "temperature": temperature,
        }
        resp = self._post("/api/chat", body)
        # 透传：成功时返回 {success, data, elapsed}，data 含 final_answer 等
        return resp

    def retrieve(
        self,
        query: str,
        k: int = 5,
        filter_: Optional[Dict[str, Any]] = None,
        score_threshold: Optional[float] = None,
    ) -> Dict[str, Any]:
        """/api/retrieve — 向量检索。"""
        body = {
            "query": query,
            "k": k,
            "filter": filter_,
            "score_threshold": score_threshold,
        }
        return self._post("/api/retrieve", body)

    def health(self) -> Dict[str, Any]:
        """/api/health — 健康检测。"""
        return self._get("/api/health")

    def upload_kb(self, files: List[Any]) -> Dict[str, Any]:
        """
        /api/kb/upload — 批量上传 PDF/docx 入库。

        Args:
            files: Streamlit UploadedFile 列表（或 (filename, bytes) 元组列表）
        """
        url = f"{self.base_url}/api/kb/upload"
        files_payload = []
        for f in files:
            # Streamlit UploadedFile 兼容：有 .read() / .name
            if hasattr(f, "read") and hasattr(f, "name"):
                files_payload.append(
                    ("files", (f.name, f.read(), "application/octet-stream"))
                )
            elif isinstance(f, tuple) and len(f) == 2:
                files_payload.append(
                    ("files", (f[0], f[1], "application/octet-stream"))
                )
        try:
            resp = requests.post(
                url,
                files=files_payload,
                timeout=max(self.timeout, 600),  # 入库耗时较长
            )
            return resp.json()
        except requests.exceptions.ConnectionError:
            return {
                "success": False,
                "error": {
                    "code": "BACKEND_OFFLINE",
                    "message": "无法连接后端服务，请确认 FastAPI 已启动。",
                    "detail": f"URL={url}",
                },
            }
        except Exception as exc:
            return {
                "success": False,
                "error": {
                    "code": "CLIENT_ERROR",
                    "message": f"上传异常：{type(exc).__name__}",
                    "detail": str(exc),
                },
            }

    # ------------------------------------------------------------------
    # 探活：用于前端启动时判断后端是否在线
    # ------------------------------------------------------------------
    def ping(self) -> bool:
        """快速探测后端是否在线（GET /）。"""
        try:
            resp = requests.get(f"{self.base_url}/", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 单例（页面层直接 import 使用）
# ---------------------------------------------------------------------------
api_client = ApiClient()


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------
def main() -> None:
    """api_client.py 自测：探测后端健康。"""
    print(f"[ApiClient] base_url = {api_client.base_url}")
    print(f"[ApiClient] 后端在线 = {api_client.ping()}")

    print("\n--- /api/health ---")
    result = api_client.health()
    print(result)


if __name__ == "__main__":
    main()
