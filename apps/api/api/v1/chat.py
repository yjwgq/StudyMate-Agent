"""POST /api/v1/chat —— M3 版本：单路 RAG 问答（带引用）。

相对 M1/M2 的演进（本里程碑落地，§8.2 / §8.4）：
    - 检索：用户有 ready 文档时自动走 RAG —— pgvector 单路向量召回子块
      → 父块回溯去重（agent/retrieval/search.py）；
    - 引用：流开始前先发 citations 事件（前端渲染脚注面板），
      助手消息落库 messages.citations（§8.4 结构）；
    - groundedness：生成完后逐句校验「无出处事实句占比」，
      > 20% 时发 notice 事件触发前端清空重生成，只重写一次；
      仍不达标 → degraded 事件 + messages.degraded 落标记（D1）；
    - 观测：每次请求一条 Langfuse trace（retrieval span + LLM generation span），
      PII 先脱敏（§13.3），未配置/失败 no-op；
    - 降级显式（§8.3）：检索失败 → degraded:retrieval，继续无 RAG 回答。

M1 保留的能力：JWT 认证、会话锁、幂等重放、失败落库、SSE 逐字回流。

后续演进（M5）：SSE Last-Event-ID 重连续传。
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field

from agent.guardrails.groundedness import check_groundedness
from agent.obs import langfuse as obs
from agent.retrieval.context import (
    NO_CONTEXT_SYSTEM_PROMPT,
    RAG_SYSTEM_PROMPT,
    REWRITE_SYSTEM_PROMPT,
    build_context_blocks,
)
from agent.retrieval.search import build_citations, get_retriever
from apps.api.core import idempotency
from apps.api.core.config import settings
from apps.api.core.db import tenant_session
from apps.api.core.deps import UserCtx, current_user
from apps.api.core.errors import AppError, ErrorCode, new_trace_id
from apps.api.core.locks import acquire_conversation_lock, release_conversation_lock
from apps.api.core.redis import get_redis
from apps.api.repositories import conversations as conv_repo
from apps.api.sse import (
    EVENT_CITATIONS,
    EVENT_DEGRADED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_NOTICE,
    EVENT_TOKEN,
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    sse_event,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000, description="用户输入")
    conversation_id: UUID | None = Field(
        default=None,
        description="会话 ID。缺省时自动创建新会话（done 事件返回新 id）。",
    )


async def get_llm_client() -> AsyncOpenAI:
    """LLM 客户端工厂。

    独立成依赖是为了可测试：集成测试用 dependency_overrides 注入 fake，
    不真调模型（设计文档 §16「集成测试用 fake LLM」）。
    """
    if not settings.llm_configured:
        raise AppError(
            ErrorCode.LLM_OFFLINE,
            "LLM 未配置：请在 .env 中填写 LLM_API_KEY 与 LLM_MODEL",
            503,
        )
    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout_s,
    )


async def _model_stream(
    client: AsyncOpenAI, messages: list[dict[str, str]]
) -> AsyncIterator[tuple[str | None, Any]]:
    """向模型发起流式请求，逐块产出 (delta, chunk)。

    异常向上抛出，由 chat() 的生成器统一转成 error 事件 ——
    HTTP 状态码在流开始时就已发出，中途断流前端只会看到「连接意外关闭」，
    拿不到任何可读信息（M0 验收 A4 的教训）。
    """
    stream = await client.chat.completions.create(
        model=settings.llm_model,
        messages=cast('list[ChatCompletionMessageParam]', messages),
        stream=True,
        # 流末尾补 usage 分片，用于 token 统计（A6）。
        # 若服务商不认该参数返回 400，删掉本行即可（token 统计随之失效）。
        stream_options={"include_usage": True},
    )
    async for chunk in stream:
        delta = None
        if chunk.choices:
            delta = chunk.choices[0].delta.content
        yield delta, chunk


@router.post("/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    user: Annotated[UserCtx, Depends(current_user)],
    llm: Annotated[AsyncOpenAI, Depends(get_llm_client)],
    idem_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
) -> StreamingResponse:
    trace_id = getattr(request.state, "trace_id", "") or new_trace_id()
    redis = get_redis()

    # ---- 1) 幂等：只读查终态，已完成 / 已失败 → 直接重放（不调模型、不落库，B7）----
    if idem_key:
        status, payload = await idempotency.peek(redis, str(user.id), idem_key)
        if status == "completed":
            return StreamingResponse(
                _replay_stream(payload, trace_id),
                media_type=SSE_MEDIA_TYPE,
                headers={**SSE_HEADERS, "X-Idempotent-Replay": "true"},
            )
        if status == "failed":
            err = (payload or {}).get("error") or {"code": "INTERNAL", "message": "上次请求失败"}
            return StreamingResponse(
                _replay_error_stream(err, trace_id),
                media_type=SSE_MEDIA_TYPE,
                headers=SSE_HEADERS,
            )
        # absent / in_progress → 继续正常流程；
        # 并发同 key 的 409 由第 4 步（锁内 acquire）统一判定

    # ---- 2) 会话归属 / 创建 ----
    if req.conversation_id is not None:
        async with tenant_session(user.id) as session:
            conv = await conv_repo.get_conversation(session, user.id, req.conversation_id)
        if conv is None:
            # RLS + 仓储过滤后查不到：不区分「不存在」与「不属于你」（B4）
            raise AppError(ErrorCode.NOT_FOUND, "会话不存在", 404)
        conversation_id = str(req.conversation_id)
    else:
        async with tenant_session(user.id) as session:
            conv = await conv_repo.create_conversation(
                session, user.id, title=req.content[:60]
            )
        conversation_id = str(conv["id"])
    cid = UUID(conversation_id)

    # ---- 3) 会话级锁（§7.4，B6）：串行独占 ----
    lock_token = await acquire_conversation_lock(redis, conversation_id)
    if lock_token is None:
        raise AppError(
            ErrorCode.CONFLICT,
            "该会话正在处理中，请稍后再试",
            409,
            headers={"Retry-After": "5"},
        )

    # ---- 4) 幂等占位（拿到锁之后再占位，避免锁冲突污染幂等状态）----
    if idem_key:
        status, payload = await idempotency.acquire(redis, str(user.id), idem_key)
        if status != "acquired":
            await release_conversation_lock(redis, str(conversation_id), lock_token)
            if status == "completed":
                return StreamingResponse(
                    _replay_stream(payload, trace_id),
                    media_type=SSE_MEDIA_TYPE,
                    headers={**SSE_HEADERS, "X-Idempotent-Replay": "true"},
                )
            raise AppError(
                ErrorCode.CONFLICT, "相同 Idempotency-Key 的请求正在处理中", 409,
                headers={"Retry-After": "3"},
            )

    # ---- 5) 用户消息落库（顺序号在锁内分配，UNIQUE(cid, seq) DB 兜底）----
    async with tenant_session(user.id) as session:
        seq = await conv_repo.next_seq(session, cid)
        await conv_repo.insert_message(
            session,
            conversation_id=cid,
            user_id=user.id,
            seq=seq,
            role="user",
            content=req.content,
            status="completed",
            trace_id=trace_id,
        )

    return StreamingResponse(
        _chat_stream(
            llm=llm,
            redis=redis,
            user=user,
            conversation_id=cid,
            content=req.content,
            user_seq=seq,
            idem_key=idem_key,
            lock_token=lock_token,
            trace_id=trace_id,
        ),
        media_type=SSE_MEDIA_TYPE,
        headers={**SSE_HEADERS, "X-Conversation-Id": conversation_id},
    )


async def _retrieve_for_chat(
    user: UserCtx, query: str
) -> tuple[list, list[dict], str, list[str]]:
    """chat 专用检索编排：embedding 在事务外，SQL 在事务内（不占死连接池）。

    返回 (hits, citations, 上下文块, degraded_flags)。任何失败都显式降级
    （§8.3：degraded:retrieval），绝不因检索失败中断聊天。
    """
    degraded: list[str] = []
    retriever = get_retriever()
    try:
        qvec = await retriever.embed_query(query)  # 网络调用，事务外
    except Exception as exc:  # noqa: BLE001
        logger.warning("query embedding 失败，降级无 RAG: %s", exc)
        return [], [], "", ["retrieval"]
    try:
        async with tenant_session(user.id) as session:
            hits = await retriever.search(session, user.id, query, qvec=qvec)
    except Exception as exc:  # noqa: BLE001
        logger.warning("检索查询失败，降级无 RAG: %s", exc)
        return [], [], "", ["retrieval"]
    citations = build_citations(hits)
    context_blocks = build_context_blocks(hits) if hits else ""
    return hits, citations, context_blocks, degraded


def _build_messages(
    content: str, context_blocks: str
) -> list[dict[str, str]]:
    """RAG 提示词拼装：有命中 → 严格引用契约；无命中 → 诚实告知模式。"""
    if context_blocks:
        system = RAG_SYSTEM_PROMPT + "\n\n可用资料：\n" + context_blocks
    else:
        system = NO_CONTEXT_SYSTEM_PROMPT
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


async def _chat_stream(
    *,
    llm: AsyncOpenAI,
    redis,
    user: UserCtx,
    conversation_id: UUID,
    content: str,
    user_seq: int,
    idem_key: str | None,
    lock_token: str,
    trace_id: str,
) -> AsyncIterator[str]:
    """执行流式回复：检索 → 引用 → 生成 → 校验（可重写一次）→ 落库 → 收尾。

    所有异常都转成 error 事件而不是断流；锁在任何路径下都会释放。
    """
    started = time.perf_counter()
    trace = obs.start_chat_trace(
        trace_id,
        user_id=str(user.id),
        query=content,
        metadata={"conversation_id": str(conversation_id)},
    )
    full_text_parts: list[str] = []
    usage_total: dict[str, int | None] = {}
    citations: list[dict] = []
    degraded_flags: list[str] = []
    grounded_stats: dict | None = None
    final_status = "completed"

    try:
        # ---- 1) 检索（显式降级：失败继续无 RAG 回答，§8.3）----
        retrieval_span = trace.span(
            "retrieval", as_type="retriever", input={"query": obs.mask_text(content)}
        )
        hits, citations, context_blocks, degraded = await _retrieve_for_chat(user, content)
        degraded_flags.extend(degraded)
        retrieval_span.update(
            output={
                "hits": len(hits),
                "top_score": hits[0].score if hits else None,
                "elapsed_s": round(time.perf_counter() - started, 3),
            }
        )
        retrieval_span.end()

        # 引用面板先于正文下发（前端先渲染脚注，再等 token 逐字回流）
        if citations:
            yield sse_event(EVENT_CITATIONS, {"citations": citations})

        # ---- 2) 第一遍生成（流式）----
        messages = _build_messages(content, context_blocks)
        gen_span = _start_generation_span(trace, "llm", messages)
        pass1_usage: dict[str, int | None] = {}
        try:
            async for delta, chunk in _model_stream(llm, messages):
                _accumulate_usage(chunk, pass1_usage)
                if delta:
                    full_text_parts.append(delta)
                    yield sse_event(EVENT_TOKEN, {"delta": delta})
        except OpenAIError as exc:
            # span 必须在所有路径 end —— 否则该 span 永不上报
            # （M3 验收：模型 429 时 Langfuse 里只有 retrieval span 没有 llm span）
            gen_span.update(level="ERROR", status_message=str(exc)[:300])
            gen_span.end()
            raise
        _merge_usage(pass1_usage, usage_total)
        _end_generation_span(gen_span, "".join(full_text_parts), pass1_usage)

        full_text = "".join(full_text_parts)
        pass1_text = full_text  # 重写失败时的回滚文本

        # ---- 3) groundedness 校验：无出处事实句 > 20% → 重写一次（§8.4）----
        # 无论是否触发重写都统计并上报（done 事件 + messages.degraded）：
        # 只在重写分支记录会让"通过"的请求看不到指标，评测与用户都无法复核。
        if citations and settings.groundedness_enabled:
            stats = check_groundedness(full_text)
            grounded_stats = stats.as_dict()
            if not stats.ok:
                yield sse_event(
                    EVENT_NOTICE,
                    {
                        "code": "GROUNDEDNESS_REWRITE",
                        "message": "部分论断缺少引用来源，正在重新生成…",
                        "clear": True,
                    },
                )
                full_text_parts = []
                rewrite_messages = [
                    {"role": "system", "content": REWRITE_SYSTEM_PROMPT + "\n\n可用资料：\n" + context_blocks},
                    {"role": "user", "content": content},
                ]
                gen_span2 = _start_generation_span(trace, "llm_rewrite", rewrite_messages)
                pass2_usage: dict[str, int | None] = {}
                try:
                    async for delta, chunk in _model_stream(llm, rewrite_messages):
                        _accumulate_usage(chunk, pass2_usage)
                        if delta:
                            full_text_parts.append(delta)
                            yield sse_event(EVENT_TOKEN, {"delta": delta})
                    _merge_usage(pass2_usage, usage_total)
                except OpenAIError as exc:
                    # 重写失败：显式标记，不中断流（span 在下方统一 end）
                    logger.warning("groundedness 重写失败: %s", exc)
                    degraded_flags.append("groundedness_rewrite_failed")
                    gen_span2.update(level="ERROR", status_message=str(exc)[:300])
                _end_generation_span(gen_span2, "".join(full_text_parts), pass2_usage)

                if not full_text_parts:
                    # 重写一个字都没出：回放第一遍结果，避免用户对着空气
                    full_text_parts = [pass1_text]
                    yield sse_event(EVENT_TOKEN, {"delta": pass1_text})
                full_text = "".join(full_text_parts)

                stats = check_groundedness(full_text)
                grounded_stats = stats.as_dict()
                if not stats.ok:
                    degraded_flags.append("groundedness")
                    yield sse_event(
                        EVENT_DEGRADED,
                        {
                            "degraded": ["groundedness"],
                            "message": "部分结论缺少资料支撑，已标注，请谨慎采信。",
                        },
                    )

        elapsed = time.perf_counter() - started
        usage_dict = usage_total or None

        # ---- 4) 助手消息落库（citations / degraded 一并持久化）----
        degraded_payload = (
            {"flags": degraded_flags, "groundedness": grounded_stats}
            if degraded_flags or grounded_stats else None
        )
        async with tenant_session(user.id) as session:
            assistant = await conv_repo.insert_message(
                session,
                conversation_id=conversation_id,
                user_id=user.id,
                seq=user_seq + 1,
                role="assistant",
                content=full_text,
                status="completed",
                token_usage=usage_dict,
                trace_id=trace_id,
                citations=citations or None,
                degraded=degraded_payload,
            )
            await conv_repo.touch_last_message(session, conversation_id)

        yield sse_event(
            EVENT_DONE,
            {
                "finish_reason": "stop",
                "conversation_id": str(conversation_id),
                "message_id": str(assistant["id"]),
                "elapsed_s": round(elapsed, 2),
                "usage": usage_dict,
                "trace_id": trace_id,
                "replayed": False,
                "citation_count": len(citations),
                "groundedness": grounded_stats,
                "degraded": degraded_flags,
            },
        )

        if idem_key:
            await idempotency.finish(
                redis, str(user.id), idem_key,
                status="completed", content=full_text,
                conversation_id=str(conversation_id), usage=usage_dict,
                citations=citations or None,
            )
        trace.end(
            output={
                "status": "completed",
                "citations": len(citations),
                "groundedness": grounded_stats,
                "degraded": degraded_flags,
                "usage": usage_dict,
            }
        )

    except OpenAIError as exc:
        final_status = "failed"
        logger.warning("chat 模型调用失败: %s", exc)
        yield sse_event(EVENT_ERROR, {"code": "LLM_OFFLINE", "message": str(exc)})
        await _persist_failed(user, conversation_id, user_seq + 1, str(exc), trace_id)
        if idem_key:
            await idempotency.finish(
                redis, str(user.id), idem_key, status="failed",
                conversation_id=str(conversation_id),
                error={"code": "LLM_OFFLINE", "message": str(exc)},
            )
        trace.end(output={"status": "failed", "error": str(exc)})
    except asyncio.CancelledError:
        # 客户端断开 / 超时取消：标记 interrupted 后原样抛出
        final_status = "interrupted"
        await _persist_failed(user, conversation_id, user_seq + 1, "客户端中断", trace_id,
                              status="interrupted")
        if idem_key:
            await idempotency.finish(
                redis, str(user.id), idem_key, status="failed",
                content="".join(full_text_parts), conversation_id=str(conversation_id),
                error={"code": "CONFLICT", "message": "生成被中断"},
            )
        trace.end(output={"status": "interrupted"})
        raise
    except Exception as exc:  # noqa: BLE001 —— 兜底，保证流一定被正常收尾
        final_status = "failed"
        logger.exception("chat 内部错误")
        yield sse_event(EVENT_ERROR, {"code": "INTERNAL", "message": str(exc)})
        await _persist_failed(user, conversation_id, user_seq + 1, str(exc), trace_id)
        if idem_key:
            await idempotency.finish(
                redis, str(user.id), idem_key, status="failed",
                conversation_id=str(conversation_id),
                error={"code": "INTERNAL", "message": str(exc)},
            )
        trace.end(output={"status": "failed", "error": str(exc)})
    finally:
        # 锁在任何路径下都释放（Lua 比对 token，防误删他人的锁）
        await release_conversation_lock(redis, str(conversation_id), lock_token)
        await obs.flush_async()
        logger.info(
            "chat done conv=%s status=%s elapsed=%.2fs",
            conversation_id, final_status, time.perf_counter() - started,
        )


def _start_generation_span(
    trace, name: str, messages: list[dict[str, str]]
) -> Any:
    return trace.span(
        name,
        as_type="generation",
        model=settings.llm_model,
        model_parameters={"temperature": 0.3},
        input=messages,
    )


def _end_generation_span(span: Any, output: str, usage: dict[str, int | None]) -> None:
    # Langfuse 的 usage_details 用 input/output/total（不是 OpenAI 的
    # prompt_tokens/completion_tokens）—— key 写错会被静默忽略，
    # trace 上就没有 token 用量（M3 验收 D3 实锤）。
    usage_details = {
        k: v for k, v in {
            "input": usage.get("prompt_tokens"),
            "output": usage.get("completion_tokens"),
            "total": usage.get("total_tokens"),
        }.items() if v is not None
    }
    span.update(output=output, usage_details=usage_details or None)
    span.end()


def _accumulate_usage(chunk: Any, acc: dict[str, int | None]) -> None:
    usage = getattr(chunk, "usage", None)
    if usage is None:
        return
    acc["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
    acc["completion_tokens"] = getattr(usage, "completion_tokens", None)
    acc["total_tokens"] = getattr(usage, "total_tokens", None)


def _merge_usage(src: dict[str, int | None], dst: dict[str, int | None]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = src.get(key)
        if val is None:
            continue
        dst[key] = (dst.get(key) or 0) + val


async def _persist_failed(
    user: UserCtx,
    conversation_id: UUID,
    seq: int,
    message: str,
    trace_id: str,
    status: str = "failed",
) -> None:
    """失败/中断的助手消息也要落库（status 标记，供断线自愈与排查）。"""
    try:
        async with tenant_session(user.id) as session:
            await conv_repo.insert_message(
                session,
                conversation_id=conversation_id,
                user_id=user.id,
                seq=seq,
                role="assistant",
                content=None,
                status=status,
                trace_id=trace_id,
                error={"message": message[:500]},
            )
            await conv_repo.touch_last_message(session, conversation_id)
    except Exception:  # noqa: BLE001 —— 失败落库本身不再抛错
        logger.exception("failed-message persist error conv=%s", conversation_id)


async def _replay_stream(payload: dict | None, trace_id: str) -> AsyncIterator[str]:
    """幂等重放：把上次完成的内容作为流再次下发（不调模型、不落库）。

    引用面板一并重放：否则用户重试同一请求会看到「答案有 [1] 但没有来源」。
    """
    data = payload or {}
    citations = data.get("citations") or []
    if citations:
        yield sse_event(EVENT_CITATIONS, {"citations": citations})
    content = data.get("content", "")
    yield sse_event(EVENT_TOKEN, {"delta": content})
    yield sse_event(
        EVENT_DONE,
        {
            "finish_reason": "stop",
            "conversation_id": (payload or {}).get("conversation_id", ""),
            "usage": (payload or {}).get("usage"),
            "trace_id": trace_id,
            "replayed": True,
            "citation_count": len(citations),
        },
    )


async def _replay_error_stream(err: dict, trace_id: str) -> AsyncIterator[str]:
    yield sse_event(
        EVENT_ERROR,
        {"code": err.get("code", "INTERNAL"), "message": err.get("message", "上次请求失败")},
    )
    yield sse_event(EVENT_DONE, {"finish_reason": "error", "replayed": True, "trace_id": trace_id})
