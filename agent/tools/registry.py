"""ToolRegistry：横切关注点的唯一收敛点（M4-6，§7.5）。

invoke 的完整管线（每个工具零成本继承）：
    策略校验（fail-closed）→ 参数校验 → 参数级风险评估（deny/升级）
    → 幂等去重（Redis，sha256 canonical args）→ 并发限流（per-tool 信号量）
    → 超时 → 结果截断（max_result_chars + full_ref）
    → 埋点（tool_invocations，E6）→ 审计（audit_logs，L1/L2 与拒绝，E7）

设计取舍与说明：
    - DB 写入（tool_invocations / audit_logs）都是 RLS 表，经 tenant_session
      以当事用户身份写入；埋点失败不阻断工具结果（旁路 + 告警日志），
      但策略拒绝的审计在抛错前尽力落库（E7 的证据链）。
    - 非幂等工具同样参与去重：相同 seed+args 的重复调用本就该挡；
      `idempotent` 标记影响的是失败后是否自动重试（ReAct 层，§7.6）。
    - L2（不可逆/外发）在 M4 显式拦截（APPROVAL_REQUIRED），M5 接入
      interrupt→approvals 后放行 —— 绝不悄悄执行不可逆操作（§7.5）。
"""

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

from agent.tools.base import BaseTool, ToolCtx, ToolError, ToolMeta, ToolResult
from agent.tools.policy import PolicyDenied, get_policy_registry

logger = logging.getLogger(__name__)

_AUDIT_TABLE_OK = True  # 占位：M9 之后若审计策略变化在此收敛


def canonical_json(args: dict[str, Any]) -> str:
    """参数规范化（§7.6）：键排序、紧凑分隔 —— 否则 {'a':1,'b':2} 与
    {'b':2,'a':1} 会被视为不同调用，幂等去重失效。"""
    return json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_dedupe_key(seed: str, tool_name: str, args: dict[str, Any]) -> str:
    """dedupe_key = sha256(seed + tool + canonical(args))（§7.6 工具层幂等）。"""
    material = f"{seed}|{tool_name}|{canonical_json(args)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def tool_to_openai_schema(meta: ToolMeta) -> dict[str, Any]:
    """把 ToolMeta 转成 OpenAI function calling schema。

    injected_params（user_id 等）从 schema 中剔除 —— LLM 看不到，
    也就无法伪造租户身份；注册表在 invoke 时从 ToolCtx 注入真实值。
    """
    schema = meta.args_schema.model_json_schema()
    schema.pop("title", None)
    props = schema.get("properties", {})
    for injected in meta.injected_params:
        props.pop(injected, None)
    if not props:
        schema.pop("properties", None)
    if schema.get("required"):
        schema["required"] = [r for r in schema["required"] if r not in meta.injected_params]
        if not schema["required"]:
            schema.pop("required", None)
    return {
        "type": "function",
        "function": {
            "name": meta.name,
            "description": meta.description,  # 已在 meta 层限长（≤500，§13.10）
            "parameters": schema,
        },
    }


class ToolRegistry:
    def __init__(self, redis: Any = None, full_ref_dir: str = "data/tool_results") -> None:
        self._tools: dict[str, BaseTool] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._redis = redis
        self._full_ref_dir = Path(full_ref_dir)
        self._policies = get_policy_registry()

    # ---------------- 注册与查询 ----------------

    def register(self, tool: BaseTool, *, require_policy: bool = True) -> None:
        name = tool.meta.name
        if name in self._tools:
            raise ValueError(f"工具重复注册：{name}")
        # §13.10 fail-closed：注册工具必须同时在策略注册表登记（测试桩可豁免）。
        # 策略缺失 = 风险等级未知 = 拒绝服务。
        if require_policy and not self._policies.is_known(name):
            raise ValueError(
                f"工具 {name!r} 未在本地策略注册表登记（§13.10 fail-closed），先在 policy.py 登记风险等级"
            )
        self._tools[name] = tool
        self._semaphores[name] = asyncio.Semaphore(tool.meta.max_concurrency)

    def get(self, name: str) -> BaseTool:
        """未注册 → TOOL_NOT_ALLOWED（fail-closed，E7）。"""
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError("TOOL_NOT_ALLOWED", f"工具 {name!r} 未注册")
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def openai_schemas(self) -> list[dict[str, Any]]:
        """给 LLM 的 function calling 工具列表（injected_params 已剔除）。"""
        return [tool_to_openai_schema(t.meta) for t in self._tools.values()]

    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    # ---------------- invoke 主管线 ----------------

    async def invoke(self, name: str, args: dict[str, Any], ctx: ToolCtx) -> ToolResult:
        started = time.perf_counter()
        tool = self._tools.get(name)
        meta = tool.meta if tool else None
        risk: int = meta.risk_level if meta else 2  # 未知工具按最高风险记录
        notes: list[str] = []
        dedupe_key: str | None = None
        result: ToolResult | None = None
        error_code: str | None = None
        truncated = False
        escalated = False
        deduped = False
        llm_args: dict[str, Any] = _safe_args(args)

        try:
            # 1) 策略层：未注册/禁用 → 拒绝（E7）
            self._policies.check_enabled(name)
            if tool is None or meta is None:
                # 策略表已登记但注册表没有执行体：同样按未注册处理
                raise ToolError("TOOL_NOT_ALLOWED", f"工具 {name!r} 未注册")

            # 2) 参数校验（pydantic，extra=forbid → 未知字段直接拒绝）
            model = meta.args_schema(**(args or {}))

            # 3) 参数级风险评估（deny / 升级到 L2）
            risk, notes = self._policies.evaluate_args(name, model)
            escalated = risk >= 2 and meta.risk_level < 2
            if risk >= 2:
                raise ToolError(
                    "APPROVAL_REQUIRED",
                    f"工具 {name} 命中 L2 风险（{'；'.join(notes) or '策略等级'}），"
                    "等待审批能力上线（M5）后执行",
                )

            # 4) 幂等去重（工具层，§7.6；命中即返回上次结果，不再执行）
            llm_args = _model_args(model, meta)
            dedupe_key = compute_dedupe_key(ctx.dedupe_seed, name, llm_args)
            cached = await self._dedupe_get(dedupe_key)
            if cached is not None:
                deduped = True
                result = cached.model_copy(update={"deduplicated": True, "dedupe_key": dedupe_key})
                return result

            # 5) 注入 ctx 参数（user_id 等，LLM 不可见、不可伪造）
            call_kwargs = dict(llm_args)
            for injected in meta.injected_params:
                call_kwargs[injected] = _injected_value(injected, name, ctx)

            # 6) 并发限流 + 超时执行
            semaphore = self._semaphores[name]
            async with semaphore:
                if ctx.emit:
                    await ctx.emit(
                        "tool_start", {"tool": name, "step": ctx.step_id, "round": ctx.round}
                    )
                try:
                    result = await asyncio.wait_for(
                        tool.arun(ctx, **call_kwargs), timeout=meta.timeout_s
                    )
                except TimeoutError:
                    raise ToolError(
                        "TOOL_TIMEOUT", f"工具 {name} 超时（>{meta.timeout_s}s）"
                    ) from None

            # 7) 结果截断（§7.8 第 1 层）
            result.risk_level = risk
            result.escalated = escalated
            result.dedupe_key = dedupe_key
            if len(result.content) > meta.max_result_chars:
                result.full_ref = self._store_full(ctx, name, result.content)
                result.content = (
                    result.content[: meta.max_result_chars]
                    + f"\n…[结果过长已截断，全文引用 full_ref={result.full_ref}，"
                    "可用 fetch_full 工具取回]"
                )
                result.truncated = True
                truncated = True

            await self._dedupe_put(dedupe_key, result)
            await self._audit_write_op(ctx, name, risk, llm_args, result)
            return result

        except PolicyDenied as exc:
            error_code = exc.code
            # E7：策略拒绝 → 审计（fail-closed 的证据链）
            await self._audit_denied(ctx, name, str(exc))
            raise
        except ToolError as exc:
            error_code = exc.code
            raise
        except Exception as exc:  # noqa: BLE001 —— 工具内部异常统一转结构化错误
            logger.exception("tool %s 执行异常", name)
            error_code = "TOOL_FAILED"
            raise ToolError("TOOL_FAILED", f"工具 {name} 执行失败：{exc}") from exc
        finally:
            latency_ms = int((time.perf_counter() - started) * 1000)
            if result is not None:
                result.latency_ms = result.latency_ms or latency_ms
            # 埋点统一在 finally：成功/失败/去重/拒绝全部有记录（E6）
            await self._record(
                ctx, name, risk, llm_args, result, deduped, started,
                error_code=error_code, truncated=truncated,
            )
            if ctx.emit:
                await ctx.emit(
                    "tool_end",
                    {
                        "tool": name,
                        "step": ctx.step_id,
                        "round": ctx.round,
                        "ok": bool(result and result.ok),
                        "error_code": error_code,
                        "truncated": truncated,
                        "deduplicated": deduped,
                        "latency_ms": latency_ms,
                        "risk_level": risk,
                        "notes": notes,
                    },
                )

    # ---------------- 内部组件 ----------------

    async def _dedupe_get(self, key: str | None) -> ToolResult | None:
        if not key or self._redis is None:
            return None
        try:
            raw = await self._redis.get(f"tool_dedupe:{key}")
            if raw:
                return ToolResult.model_validate_json(raw)
        except Exception:  # noqa: BLE001 —— Redis 故障不阻断执行
            logger.warning("dedupe get 失败（忽略）", exc_info=True)
        return None

    async def _dedupe_put(self, key: str | None, result: ToolResult) -> None:
        if not key or self._redis is None:
            return
        try:
            await self._redis.set(f"tool_dedupe:{key}", result.model_dump_json(), ex=86400)
        except Exception:  # noqa: BLE001
            logger.warning("dedupe put 失败（忽略）", exc_info=True)

    def _store_full(self, ctx: ToolCtx, name: str, content: str) -> str:
        """超长全文落盘（§7.5 说对象存储/表；M4 用本地卷 + 引用 id，fetch_full 取回）。"""
        self._full_ref_dir.mkdir(parents=True, exist_ok=True)
        ref = f"{ctx.user_id[:8]}-{name}-{hashlib.sha1(content.encode()).hexdigest()[:12]}.txt"
        (self._full_ref_dir / ref).write_text(content, encoding="utf-8")
        return ref

    async def _record(
        self,
        ctx: ToolCtx,
        name: str,
        risk: int,
        args: dict[str, Any],
        result: ToolResult | None,
        deduped: bool,
        started: float,
        *,
        error_code: str | None = None,
        truncated: bool = False,
    ) -> None:
        """tool_invocations 埋点（E6）。任何路径都恰好记录一次。"""
        latency_ms = int((time.perf_counter() - started) * 1000)
        row = {
            "tool": name,
            "risk": risk,
            "args": canonical_json(args)[:2000],
            "ok": bool(result and result.ok),
            "error_code": error_code,
            "dedup": deduped,
            "latency": latency_ms,
            "chars": len(result.content) if result else 0,
            "truncated": truncated or bool(result and result.truncated),
        }
        events = ctx.extra.setdefault("tool_events", [])
        if isinstance(events, list):
            events.append(row)
        try:
            from apps.api.core.db import tenant_session

            if not ctx.user_id:
                return
            async with tenant_session(UUID(ctx.user_id)) as session:
                await session.execute(
                    text("""
                        INSERT INTO tool_invocations
                          (user_id, message_id, trace_id, step_id, round, tool_name,
                           risk_level, args, result_ok, error_code, deduplicated,
                           latency_ms, result_chars, truncated)
                        VALUES (:uid, CAST(NULLIF(:mid,'') AS UUID), :trace, :step, :round,
                                :tool, :risk, CAST(:args AS JSONB), :ok, :err, :dedup,
                                :latency, :chars, :truncated)
                    """),
                    {
                        "uid": ctx.user_id,
                        "mid": ctx.message_id or "",
                        "trace": ctx.trace_id,
                        "step": ctx.step_id,
                        "round": ctx.round,
                        "tool": name,
                        "risk": risk,
                        "args": row["args"],
                        "ok": row["ok"],
                        "err": error_code,
                        "dedup": deduped,
                        "latency": latency_ms,
                        "chars": row["chars"],
                        "truncated": row["truncated"],
                    },
                )
        except Exception:  # noqa: BLE001 —— 埋点旁路
            logger.warning("tool_invocations 埋点失败 tool=%s", name, exc_info=True)

    async def _audit_denied(self, ctx: ToolCtx, name: str, why: str) -> None:
        """策略拒绝审计（E7）。失败不改变拒绝结果。"""
        try:
            from apps.api.core.db import tenant_session

            if not ctx.user_id:
                return
            async with tenant_session(UUID(ctx.user_id)) as session:
                await session.execute(
                    text("""
                        INSERT INTO audit_logs (user_id, actor_type, action, target, payload, trace_id)
                        VALUES (:uid, 'user', 'tool.denied', :target,
                                CAST(:payload AS JSONB), :trace)
                    """),
                    {
                        "uid": ctx.user_id,
                        "target": name,
                        "payload": json.dumps({"reason": why[:500]}, ensure_ascii=False),
                        "trace": ctx.trace_id,
                    },
                )
        except Exception:  # noqa: BLE001
            logger.warning("denied 审计写入失败 tool=%s", name, exc_info=True)

    async def _audit_write_op(
        self, ctx: ToolCtx, name: str, risk: int, args: dict[str, Any], result: ToolResult
    ) -> None:
        """L1/L2 写操作的审计（§13.12；幂等键一并记录）。"""
        if risk < 1 or not result.ok:
            return
        try:
            from apps.api.core.db import tenant_session

            if not ctx.user_id:
                return
            async with tenant_session(UUID(ctx.user_id)) as session:
                await session.execute(
                    text("""
                        INSERT INTO audit_logs (user_id, actor_type, action, target, payload, trace_id)
                        VALUES (:uid, 'user', 'tool.executed', :target,
                                CAST(:payload AS JSONB), :trace)
                    """),
                    {
                        "uid": ctx.user_id,
                        "target": name,
                        "payload": json.dumps(
                            {
                                "risk_level": risk,
                                "dedupe_key": result.dedupe_key,
                                "args": canonical_json(args)[:1000],
                            },
                            ensure_ascii=False,
                        ),
                        "trace": ctx.trace_id,
                    },
                )
        except Exception:  # noqa: BLE001
            logger.warning("write-op 审计写入失败 tool=%s", name, exc_info=True)


def _model_args(model: Any, meta: ToolMeta) -> dict[str, Any]:
    """校验后的参数（剔除注入参数，仅 LLM 声明的部分参与幂等键）。"""
    data = model.model_dump()
    for injected in meta.injected_params:
        data.pop(injected, None)
    return data


def _injected_value(param: str, tool_name: str, ctx: ToolCtx) -> Any:
    """从 ToolCtx 解析注入参数值（只允许白名单内字段，防越权注入）。"""
    mapping: dict[str, Any] = {
        "user_id": ctx.user_id,
        "conversation_id": ctx.conversation_id,
        "message_id": ctx.message_id,
        "dedupe_key": compute_dedupe_key(
            ctx.dedupe_seed or f"{ctx.conversation_id}:{ctx.step_id}:{ctx.round}",
            tool_name,
            {},
        ),
    }
    if param not in mapping:
        raise ToolError("TOOL_FAILED", f"未知注入参数 {param}")
    return mapping[param]


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    return args if isinstance(args, dict) else {}
