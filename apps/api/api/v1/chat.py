"""POST /api/v1/chat —— M0 版本。

M0 刻意做到最简：**不接数据库、不接 RAG、不接 Agent**。
它唯一的使命是验证「浏览器 → FastAPI → DeepSeek → SSE 逐字回流」这条链路是通的，
把 Docker 网络 / API Key / SSE 缓冲这三类最容易卡住的环境问题提前排除掉。

后续演进：
    M3 → 接入检索链路（答案带引用）
    M4 → 接入 Agent 运行时（Planner + ReAct + 工具）
    M5 → 改为「投递任务 + 订阅流」，支持断线重连与取消
"""

import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, Field

from apps.api.core.config import settings
from apps.api.sse import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_TOKEN,
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    sse_event,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

SYSTEM_PROMPT = "你是一个乐于助人的中文 AI 助理。回答简洁准确，不确定时如实说明。"


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000, description="用户输入")
    conversation_id: str | None = Field(
        default=None,
        description="会话 ID。M0 未使用（尚无数据库），M1 起用于多轮上下文与租户隔离。",
    )


async def _stream_reply(client: AsyncOpenAI, req: ChatRequest) -> AsyncIterator[str]:
    """向 DeepSeek 发起流式请求，把增量逐块转成 SSE 事件。

    这里的关键设计：**所有异常都转换成 error 事件，而不是让连接断在半途**。
    因为 HTTP 状态码在流开始时就已发出，之后再抛异常前端只会看到「连接意外关闭」，
    拿不到任何可读信息 —— 验收项 A4 检查的正是这一点。
    """
    started = time.perf_counter()
    usage = None

    try:
        stream = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": req.content},
            ],
            stream=True,
            # 让服务端在流末尾补一个 usage 分片，用于日志里的 token 统计（验收项 A6）。
            # 若某服务商不认这个参数并返回 400，删掉本行即可（token 统计随之失效）。
            stream_options={"include_usage": True},
        )

        async for chunk in stream:
            # 注意顺序：usage 分片的 choices 是空数组，
            # 必须先取 usage 再判断 choices，否则会漏掉它。
            if chunk.usage:
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield sse_event(EVENT_TOKEN, {"delta": delta})

        elapsed = time.perf_counter() - started
        logger.info(
            "chat 完成 elapsed=%.2fs prompt_tokens=%s completion_tokens=%s",
            elapsed,
            getattr(usage, "prompt_tokens", "?"),
            getattr(usage, "completion_tokens", "?"),
        )
        yield sse_event(
            EVENT_DONE,
            {
                "finish_reason": "stop",
                "elapsed_s": round(elapsed, 2),
                "usage": {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                },
            },
        )

    except OpenAIError as exc:
        logger.warning("chat 模型调用失败: %s", exc)
        yield sse_event(EVENT_ERROR, {"code": "LLM_OFFLINE", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — 兜底，保证流一定被正常收尾
        logger.exception("chat 内部错误")
        yield sse_event(EVENT_ERROR, {"code": "INTERNAL", "message": str(exc)})


@router.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    """流式对话。返回 text/event-stream。"""
    # 配置缺失必须在「建流之前」检查：此时还能返回规范的 JSON 错误体，
    # 一旦开始流式响应，HTTP 状态码就固定为 200 了。
    if not settings.llm_configured:
        raise HTTPException(
            status_code=503,
            detail="LLM 未配置：请在 .env 中填写 LLM_API_KEY 与 LLM_MODEL",
        )

    client = AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout_s,
    )
    return StreamingResponse(
        _stream_reply(client, req),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )
