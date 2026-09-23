"""Langfuse trace 封装（M3-4，§14.1）。

设计取舍：
    - 手动 span 而不是 openai 自动插桩：我们只在**脱敏后**才把内容交给
      SDK（§13.3），自动插桩会把原始 prompt 直接序列化上报，脱敏时机失控；
    - 双保险：手动 mask_text 之外，再给 Langfuse 传 `mask=` 回调兜底
      （覆盖 SDK 内部所有序列化路径），tests/unit/test_redact.py 断言两层都生效；
    - 未配置 / 初始化失败一律 no-op：观测是旁路，绝不能阻塞聊天主链路。
      与 §8.3「降级必须显式」不冲突 —— 观测丢失不影响用户可见行为，
      但会打 warning 日志，部署时能发现。

trace 结构（每次 chat 一条 trace）：
    chat (span, trace root)
      ├─ retrieval (retriever span)：query → 命中数/最高分/耗时
      └─ llm (generation)：model / messages / usage / 输出全文
        （引用不足触发重写时记两次 generation：llm 与 llm_rewrite）
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from agent.obs.redact import mask_any, mask_text

logger = logging.getLogger(__name__)


@dataclass
class _NoopObservation:
    """Langfuse 未配置/未启用时的占位对象：接口同形，全部 no-op。"""

    def update(self, **_: Any) -> "_NoopObservation":
        return self

    def end(self) -> None:
        return None


@dataclass
class ChatTrace:
    """一次 chat 请求的 trace 句柄（root span + 子 span 工厂）。"""

    trace_id: str
    _root: Any = field(default_factory=_NoopObservation)
    _client: Any = None
    enabled: bool = False

    def span(self, name: str, *, as_type: str = "span", **kwargs: Any) -> Any:
        if not self.enabled or self._root is None:
            return _NoopObservation()
        try:
            return self._root.start_observation(name=name, as_type=as_type, **kwargs)
        except Exception:  # noqa: BLE001 —— 观测失败不影响业务
            logger.warning("langfuse span %s 创建失败", name, exc_info=True)
            return _NoopObservation()

    def end(self, output: dict[str, Any] | None = None) -> None:
        """结束 root span（不阻塞事件循环）。"""
        if not self.enabled or self._root is None:
            return
        try:
            self._root.update(output=mask_any(output or {}))
            self._root.end()
            if self._client is not None:
                # Langfuse v4 只有同步 flush()；事件循环里放线程池，
                # 同步上下文（脚本/单测/CLI）直接调用
                try:
                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(None, self._client.flush)
                except RuntimeError:
                    self._client.flush()
        except Exception:  # noqa: BLE001
            logger.warning("langfuse trace 收尾失败", exc_info=True)


_client = None
_client_init_done = False

# Langfuse 把 input/output/metadata 写进 OTel span attribute，key 形如
# 'langfuse.observation.input' / 'langfuse.trace.output'。mask 回调在
# **导出前**逐个 span 调用，只能改 attribute —— 这是 trace 通道的最后一道闸门。
_MASK_ATTR_SUFFIXES = (".input", ".output", ".metadata")


def mask_hook(*, data: Any) -> Any:
    """Langfuse `mask=` 回调（SDK 写入时调用，签名固定为 `data=` 关键字）。

    覆盖范围：我们通过 start_observation()/update() 主动写入的
    input/output/metadata —— 见 SDK 源码 `self._langfuse_client._mask(data=data)`。
    初版签名写成位置参数导致 SDK 抛 TypeError 并退回内置 fallback（M3 验收踩坑），
    单测 test_redact.py::TestMaskHooks 锁死签名。
    """
    return mask_any(data)


def mask_otel_spans(*, params: Any = None, spans: Any = None) -> Any:
    """Langfuse `mask_otel_spans=` 回调：导出前兜底（含第三方插桩的 span）。

    SDK 契约（langfuse.types，dataclass）：
        mask_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult
        MaskOtelSpansResult(span_patches={identifier: OtelSpanPatch(...)})
        OtelSpanPatch(set_attributes={...}, delete_attributes=[...])

    两个必须遵守、否则**整批 trace 被丢弃**的硬约束（M3 验收踩坑记录）：
        1. 返回的必须是 MaskOtelSpansResult 实例，普通 dict 会被判为非法
           并 drop 整个 export batch（比脱敏失败更严重：数据全丢）；
        2. patch 必须是 OtelSpanPatch 实例，否则该 span 的 patch 被丢弃。
    因此本函数在任何异常情况下返回 **None** —— SDK 约定 None = 不做任何修改，
    数据原样通过（fail-open 到「不脱敏」而不是 fail-open 到「丢数据」；
    真正的内容脱敏由 mask_hook 在写入阶段完成，这里是第二道闸门）。

    为什么需要第二道闸门（§13.3）：mask_hook 只覆盖 SDK API 写入路径；
    自动插桩（openai instrumentor 等）产生的 span 只能在这条导出通道兜住。
    """
    try:
        from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

        container = params if params is not None else spans
        span_map = getattr(container, "spans", None)
        if span_map is None:
            span_map = container or {}

        patches: dict[Any, Any] = {}
        for ident, span in span_map.items():
            attrs = getattr(span, "attributes", None)
            if attrs is None and isinstance(span, dict):
                attrs = span.get("attributes")
            attrs = attrs or {}
            set_attrs: dict[str, Any] = {}
            for key, value in attrs.items():
                if not key.endswith(_MASK_ATTR_SUFFIXES):
                    continue
                masked = _mask_attr_value(value)
                if masked != value:
                    set_attrs[key] = masked
            if set_attrs:
                patches[ident] = OtelSpanPatch(
                    set_attributes=set_attrs, delete_attributes=[]
                )
        return MaskOtelSpansResult(span_patches=patches)
    except Exception:  # noqa: BLE001 —— 回调绝不能抛，也不会因类型不符丢数据
        logger.warning("mask_otel_spans 处理失败，本轮不做 patch", exc_info=True)
        return None


def _mask_attr_value(value: Any) -> Any:
    """attribute 值多为 JSON 字符串；解析后递归脱敏再序列化回去。

    解析失败（非 JSON）则按纯文本脱敏 —— 宁可多脱不可漏。
    非字符串标量原样透传（int/bool 不承载 PII）。
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "["):
            try:
                return json.dumps(mask_any(json.loads(value)), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        return mask_text(value)
    if isinstance(value, (list, tuple)):
        return [mask_text(v) if isinstance(v, str) else v for v in value]
    return value


def _get_client() -> Any:
    """惰性单例。首次调用时初始化；失败返回 None 并记住，不再重试。"""
    global _client, _client_init_done
    if _client_init_done:
        return _client
    _client_init_done = True
    try:
        from apps.api.core.config import settings

        if not settings.langfuse_configured:
            return None

        from langfuse import Langfuse

        kwargs: dict[str, Any] = {
            "host": settings.langfuse_host,
            "sample_rate": settings.langfuse_sample_rate,
            # §13.3 双钩子：mask 管 SDK 写入路径，mask_otel_spans 管导出路径
            "mask": mask_hook,
            "mask_otel_spans": mask_otel_spans,
        }
        if settings.app_env != "dev":
            kwargs["environment"] = settings.app_env
        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            **kwargs,
        )
        logger.info("langfuse 已启用 host=%s", settings.langfuse_host)
    except Exception:  # noqa: BLE001 —— 观测是旁路
        logger.warning("langfuse 初始化失败，观测降级为 no-op", exc_info=True)
        _client = None
    return _client


def start_chat_trace(
    trace_id: str,
    *,
    user_id: str,
    query: str,
    metadata: dict[str, Any] | None = None,
) -> ChatTrace:
    """开始一条 chat trace。未配置/失败时返回 enabled=False 的句柄。"""
    client = _get_client()
    if client is None:
        return ChatTrace(trace_id=trace_id)
    try:
        root = client.start_observation(
            name="chat",
            as_type="span",
            trace_context={"trace_id": trace_id},
            input={"query": mask_text(query)},
            metadata={"user_id": user_id, **(metadata or {})},
        )
        _set_trace_attrs(root, user_id=user_id, session_id=(metadata or {}).get("conversation_id"))
        return ChatTrace(trace_id=trace_id, _root=root, _client=client, enabled=True)
    except Exception:  # noqa: BLE001
        logger.warning("langfuse trace 创建失败", exc_info=True)
        return ChatTrace(trace_id=trace_id)


def _set_trace_attrs(root: Any, *, user_id: str | None, session_id: Any) -> None:
    """设置 trace 级 user_id / session_id（Langfuse 的按用户/会话聚合依赖它们）。

    v4 的公开入口 `propagate_attributes` 是 context manager（依赖 OTel context
    attach/detach）；我们的 trace 建在 async 生成器里，context 跨越 await
    传播不可靠，因此直接在 root 的 OTel span 上写 attribute。
    失败只告警：观测属性缺失不影响业务（D3 只要求 span/token/耗时可查）。
    """
    try:
        from langfuse._client.client import LangfuseOtelSpanAttributes as Attrs

        otel_span = getattr(root, "_otel_span", None)
        if otel_span is None:
            return
        if user_id:
            otel_span.set_attribute(Attrs.TRACE_USER_ID, user_id)
        if session_id:
            otel_span.set_attribute(Attrs.TRACE_SESSION_ID, str(session_id))
    except Exception:  # noqa: BLE001
        logger.debug("trace 属性设置失败（忽略）", exc_info=True)


async def flush_async() -> None:
    """异步 flush（事件循环里调用；未启用时 no-op）。

    Langfuse v4 只有同步 flush()，会阻塞事件循环，故放线程池。
    """
    if _client is None:
        return
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _client.flush)
    except Exception:  # noqa: BLE001
        logger.debug("langfuse flush 失败（忽略）")


__all__ = [
    "ChatTrace",
    "flush_async",
    "mask_any",
    "mask_hook",
    "mask_otel_spans",
    "mask_text",
    "start_chat_trace",
]
