"""LLM 提供商层（M4）：Agent 图内所有模型调用的唯一入口。

为什么包一层而不是直接用 AsyncOpenAI：
    1. 兼容测试 fake —— M1 的 FakeLLM 模拟的是「chat.completions.create
       返回异步分块流」，真实 SDK 在 stream=False 时返回 Response 对象。
       本层统一消化两种形态，测试 fake 无需感知工具调用协议；
    2. 统一 usage 统计与 Langfuse generation span（工具调用也要观测）；
    3. 节点代码只依赖 `AgentLLM` 协议，方便 M4-12 用 fake LLM 跑全图。

设计取舍：ReAct 内的工具决策用非流式 complete（需要 tool_calls 结构化
结果）；用户可见文本（chat 分支 / synthesize）用 stream 逐字回流 SSE。
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.obs import langfuse as obs

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """模型给出的工具调用请求（OpenAI 形态的简化版）。"""

    id: str
    name: str
    arguments: dict[str, Any]  # 已 json.loads；解析失败保留原始串在 raw_arguments


@dataclass
class LLMResult:
    """一次非流式调用的结果。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int | None] = field(default_factory=dict)
    finish_reason: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMProtocol(Protocol):
    """Agent 图对 LLM 的最小依赖面（测试 fake 实现同款签名即可）。"""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        span_name: str = "llm",
    ) -> LLMResult: ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
        span_name: str = "llm",
    ) -> AsyncIterator[tuple[str | None, dict[str, int | None]]]: ...


class AgentLLM:
    """包装 AsyncOpenAI 兼容客户端（真实 SDK 或测试 fake 均可）。"""

    def __init__(self, client: Any, model: str, *, trace: Any = None) -> None:
        self._client = client
        self.model = model
        self._trace = trace  # ChatTrace 句柄（可为 no-op）

    # ---- 内部：兼容「Response 对象」与「异步分块流」两种返回形态 ----
    async def _create(self, **kwargs: Any) -> Any:
        return await self._client.chat.completions.create(**kwargs)

    async def _consume_stream(self, resp: Any) -> tuple[str, dict[str, int | None], str]:
        """把流式响应聚合为 (text, usage, finish_reason)。"""
        parts: list[str] = []
        usage: dict[str, int | None] = {}
        finish = ""
        async for chunk in resp:
            if getattr(chunk, "usage", None):
                usage = {
                    "prompt_tokens": getattr(chunk.usage, "prompt_tokens", None),
                    "completion_tokens": getattr(chunk.usage, "completion_tokens", None),
                    "total_tokens": getattr(chunk.usage, "total_tokens", None),
                }
            choices = getattr(chunk, "choices", None)
            if choices:
                delta = getattr(choices[0], "delta", None)
                piece = getattr(delta, "content", None) if delta else None
                if piece:
                    parts.append(piece)
                if getattr(choices[0], "finish_reason", None):
                    finish = choices[0].finish_reason
        return "".join(parts), usage, finish

    @staticmethod
    def _parse_tool_calls(message: Any) -> list[ToolCall]:
        out: list[ToolCall] = []
        for tc in getattr(message, "tool_calls", None) or []:
            raw = getattr(tc, "function", None)
            if raw is None:
                continue
            args_raw = getattr(raw, "arguments", "{}") or "{}"
            try:
                args = json.loads(args_raw)
                if not isinstance(args, dict):
                    args = {"_raw": args}
            except (ValueError, TypeError):
                args = {}
            out.append(ToolCall(id=getattr(tc, "id", "") or "", name=getattr(raw, "name", "") or "", arguments=args))
        return out

    def _span(self, name: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None):
        if self._trace is None:
            return obs._NoopObservation()
        return self._trace.span(
            name,
            as_type="generation",
            model=self.model,
            model_parameters={"temperature": 0.3},
            input=messages,
            metadata={"tools": [t["function"]["name"] for t in tools]} if tools else None,
        )

    @staticmethod
    def _end_span(span: Any, output: str, usage: dict[str, int | None]) -> None:
        usage_details = {
            k: v
            for k, v in {
                "input": usage.get("prompt_tokens"),
                "output": usage.get("completion_tokens"),
                "total": usage.get("total_tokens"),
            }.items()
            if v is not None
        }
        span.update(output=output[:2000], usage_details=usage_details or None)
        span.end()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        span_name: str = "llm",
    ) -> LLMResult:
        """非流式调用（planner / ReAct 决策）。工具结果也在 usage 里返回。"""
        span = self._span(span_name, messages, tools)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            resp = await self._create(**kwargs)
        except Exception as exc:
            span.update(level="ERROR", status_message=str(exc)[:300])
            span.end()
            raise

        # 形态 1：Response 对象（stream=False）
        if hasattr(resp, "choices") and not hasattr(resp, "__anext__"):
            message = resp.choices[0].message
            text = getattr(message, "content", None) or ""
            usage_raw = getattr(resp, "usage", None)
            usage = {
                "prompt_tokens": getattr(usage_raw, "prompt_tokens", None),
                "completion_tokens": getattr(usage_raw, "completion_tokens", None),
                "total_tokens": getattr(usage_raw, "total_tokens", None),
            } if usage_raw else {}
            result = LLMResult(
                text=text,
                tool_calls=self._parse_tool_calls(message),
                usage=usage,
                finish_reason=getattr(resp.choices[0], "finish_reason", "") or "",
            )
        else:
            # 形态 2：异步分块流（测试 fake 的流式接口）——聚合后按非流式语义返回
            text, usage, finish = await self._consume_stream(resp)
            result = LLMResult(text=text, usage=usage, finish_reason=finish)
        self._end_span(span, result.text, result.usage)
        return result

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
        span_name: str = "llm",
    ) -> AsyncIterator[tuple[str | None, dict[str, int | None]]]:
        """流式调用：逐块产出 (delta, usage_若有)。用于用户可见文本。"""
        span = self._span(span_name, messages, None)
        parts: list[str] = []
        usage: dict[str, int | None] = {}
        try:
            resp = await self._create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                stream=True,
                stream_options={"include_usage": True},
            )
            # 统一走流式消费（若 fake 返回 Response 对象则退化为整体下发）
            if hasattr(resp, "__anext__"):
                async for chunk in resp:
                    if getattr(chunk, "usage", None):
                        usage = {
                            "prompt_tokens": getattr(chunk.usage, "prompt_tokens", None),
                            "completion_tokens": getattr(chunk.usage, "completion_tokens", None),
                            "total_tokens": getattr(chunk.usage, "total_tokens", None),
                        }
                        yield None, usage
                    choices = getattr(chunk, "choices", None)
                    if choices:
                        delta = getattr(choices[0], "delta", None)
                        piece = getattr(delta, "content", None) if delta else None
                        if piece:
                            parts.append(piece)
                            yield piece, {}
            else:
                message = resp.choices[0].message
                text = getattr(message, "content", None) or ""
                parts.append(text)
                yield text, {}
        except Exception as exc:
            span.update(level="ERROR", status_message=str(exc)[:300])
            span.end()
            raise
        span.update(output="".join(parts)[:2000], usage_details=_usage_details(usage) or None)
        span.end()


def _usage_details(usage: dict[str, int | None]) -> dict[str, int]:
    return {
        k: v
        for k, v in {
            "input": usage.get("prompt_tokens"),
            "output": usage.get("completion_tokens"),
            "total": usage.get("total_tokens"),
        }.items()
        if v is not None
    }



def build_agent_llm(client: Any, model: str, trace: Any = None) -> AgentLLM:
    return AgentLLM(client, model, trace=trace)
