"""工具协议三层拆分（M4-6，§7.5）：元数据 + 执行体 + 注册表。

设计要点（§7.5 v1.1 重写的原因）：
    v1 把 `async def arun` 塞进 BaseModel，让数据校验模型承载执行逻辑；
    超时/重试/幂等/风险拦截散落在各工具中。v2 拆为三层后，新增工具只需
    实现 `arun` + 声明 `meta`，其余横切关注点由 ToolRegistry 零成本继承。

风险分级（§7.5 表）：
    L0 只读（search/retrieve/math）→ 自动执行
    L1 写操作（创建 todo）→ 执行 + 事后告知（含幂等键）
    L2 不可逆/外发 → interrupt → 审批（M5 落地；M4 注册表会拒绝注册未接线的 L2）

默认值偏保守（§13.10 fail-closed 精神）：
    risk_level 默认 2（未声明即最高风险）、idempotent 默认 False（强制想清楚副作用）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolArgs(BaseModel):
    """工具参数基类：具体工具继承并声明字段（pydantic 校验 + LLM schema 生成）。"""

    model_config = ConfigDict(extra="forbid")  # 未知参数直接拒绝（防注入未知字段）


class ArgPolicy(BaseModel):
    """参数级风险规则（§7.5）。

    v1.1 用字符串表达式，但 `eval` 不可接受；v2 改为**代码内声明**的
    校验器函数（在 policy.py 里定义，可被单测覆盖）。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # 校验器是 ArgsRule 实例列表（代码内声明，不用 eval 字符串表达式）
    deny: list[Any] = Field(default_factory=list)
    escalate_to_l2: list[Any] = Field(default_factory=list)


class ArgsRule(ABC):
    """参数规则：返回 (命中, 说明)。"""

    @abstractmethod
    def check(self, args: BaseModel) -> tuple[bool, str]: ...


class ToolMeta(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str = Field(max_length=500)   # §13.10：描述进 function calling schema，是注入载体
    args_schema: type[ToolArgs]
    risk_level: Literal[0, 1, 2] = 2           # 默认最高风险：未声明即保守
    idempotent: bool = False                   # 默认非幂等：强制提供 dedupe 逻辑
    timeout_s: float = 15.0
    max_result_chars: int = 4000
    max_concurrency: int = 3
    args_policy: ArgPolicy | None = None
    source: Literal["builtin", "mcp"] = "builtin"
    mcp_server: str | None = None
    injected_params: list[str] = Field(default_factory=list)
    # injected_params：由注册表从 ToolCtx 注入的参数（user_id/dedupe_key 等），
    # **不出现在给 LLM 的 schema 里**，LLM 无法伪造租户身份


@dataclass
class ToolCtx:
    """一次工具调用的运行时上下文（注册表构造，工具实现只读）。"""

    user_id: str
    conversation_id: str
    message_id: str = ""
    trace_id: str = ""
    step_id: int = 0
    round: int = 0
    dedupe_seed: str = ""      # thread+step+round，注册表用来算 dedupe_key
    emit: Any = None           # async callback(event: str, data: dict) —— tool_start/tool_end
    extra: dict[str, Any] = field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool
    content: str
    full_ref: str | None = None      # 超长内容的存储引用（§7.8，fetch_full 可取回）
    truncated: bool = False
    error_code: str | None = None
    latency_ms: int = 0
    data: dict[str, Any] | None = None   # 结构化负载（如检索命中），与 content 并存
    # 风险与幂等标记（注册表填写，工具实现不用管）
    dedupe_key: str | None = None
    deduplicated: bool = False
    risk_level: int = 0
    escalated: bool = False          # 参数级策略把 L0/L1 升到了 L2


class ToolError(Exception):
    """工具执行失败（注册表已记录埋点后抛出，ReAct 把 message 作为 Observation）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ApprovalInterrupt(Exception):
    """L2 工具等待人工审批（M5-2）。

    registry 落 approvals 行（pending）后抛出；react 层捕获并调
    LangGraph interrupt() 暂停图（§7.3：risk=L2 → interrupt → approvals）。
    审批决策经 Command(resume=...) 回流：approved → 重调 invoke（ctx 带
    approved_approval_id）；rejected → 作为 Observation 回灌给模型。
    """

    def __init__(
        self, *, approval_id: str, tool_name: str, tool_args: dict, risk_level: int, reason: str
    ) -> None:
        # 注意：不能用 self.args —— Exception.args 是内建元组属性
        super().__init__(f"工具 {tool_name} 等待审批（{approval_id}）")
        self.approval_id = approval_id
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.risk_level = risk_level
        self.reason = reason


class BaseTool(ABC):
    """执行体（ABC 强制实现）。元数据在 meta，横切关注点在 ToolRegistry。"""

    meta: ToolMeta

    @abstractmethod
    async def arun(self, ctx: ToolCtx, **kwargs: Any) -> ToolResult: ...
