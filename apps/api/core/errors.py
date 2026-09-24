"""统一错误码字典（设计文档 v1.1 附录 C）与异常处理。

响应格式契约（§11.1）：
    成功：{"data": ..., "trace_id": "..."}
    失败：{"error": {"code", "message", "detail"}, "trace_id": "..."}

用法：业务代码 raise AppError(ErrorCode.CONFLICT, "该会话正在处理中", 409)，
由 main.py 注册的 handler 统一渲染为上面的失败格式。
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorCode:
    """错误码字典 —— 全集见附录 C；M1 使用其中一部分，其余供后续里程碑引用。"""

    # 认证与授权
    AUTH_FAILED = "AUTH_FAILED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_REUSE_DETECTED = "TOKEN_REUSE_DETECTED"
    FORBIDDEN = "FORBIDDEN"
    # 请求语义
    CONFLICT = "CONFLICT"
    VALIDATION = "VALIDATION"
    NOT_FOUND = "NOT_FOUND"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    # 依赖与系统
    LLM_OFFLINE = "LLM_OFFLINE"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    INTERNAL = "INTERNAL"
    # 后续里程碑使用（此处仅占位，保持字典完整）
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    RETRIEVAL_DEGRADED = "RETRIEVAL_DEGRADED"
    RERANK_UNAVAILABLE = "RERANK_UNAVAILABLE"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_FAILED = "TOOL_FAILED"
    MCP_SERVER_DOWN = "MCP_SERVER_DOWN"
    SANDBOX_ERROR = "SANDBOX_ERROR"
    SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"
    SEARCH_FAILED = "SEARCH_FAILED"
    UPLOAD_REJECTED = "UPLOAD_REJECTED"
    PARSE_FAILED = "PARSE_FAILED"


class AppError(Exception):
    """业务异常：携带错误码 + HTTP 状态 + 可选响应头（如 Retry-After）。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = status.HTTP_400_BAD_REQUEST,
        *,
        detail: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail
        self.headers = headers


def new_trace_id() -> str:
    return uuid.uuid4().hex


def error_payload(code: str, message: str, detail: Any = None, trace_id: str = "") -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        err["detail"] = detail
    return {"error": err, "trace_id": trace_id}


def register_exception_handlers(app: FastAPI) -> None:
    """把三类异常统一渲染为 §11.1 的失败格式。"""

    @app.exception_handler(AppError)
    async def handle_app_error(
        request: Request, exc: AppError
    ) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "")
        logger.warning(
            "app_error code=%s status=%s path=%s trace=%s msg=%s",
            exc.code, exc.http_status, request.url.path, trace_id, exc.message,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=error_payload(exc.code, exc.message, exc.detail, trace_id),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "")
        detail = [
            {"field": ".".join(str(x) for x in e.get("loc", [])), "msg": e.get("msg", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_payload(
                ErrorCode.VALIDATION, "参数校验失败", detail, trace_id
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "")
        code_by_status = {
            401: ErrorCode.AUTH_FAILED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            409: ErrorCode.CONFLICT,
        }
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                code_by_status.get(exc.status_code, ErrorCode.INTERNAL),
                str(exc.detail),
                None,
                trace_id,
            ),
            headers=getattr(exc, "headers", None),
        )

    @app.middleware("http")
    async def catch_all_errors(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        """兜底：未预期异常统一 500 INTERNAL（记录完整堆栈，不向客户端泄漏细节）。"""
        try:
            return await call_next(request)
        except AppError:
            raise  # 已由上面的 handler 处理
        except Exception:  # noqa: BLE001 —— 兜底
            trace_id = getattr(request.state, "trace_id", "")
            logger.exception("unhandled error trace=%s path=%s", trace_id, request.url.path)
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content=error_payload(
                    ErrorCode.INTERNAL, "服务器内部错误，请稍后重试", None, trace_id
                ),
            )
