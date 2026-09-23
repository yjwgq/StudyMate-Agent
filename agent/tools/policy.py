"""本地风险策略注册表（M4-7，§13.10 / ADR-4）。

核心原则：**fail-closed** ——
    1. 未注册的工具默认拒绝（ToolNotAllowed），注册表之外没有信任；
    2. 风险等级来自本地定义，**不信任工具自述**（MCP 是开放协议，
       第三方 server 的 self-reported risk_level 只能当参考）；
    3. 参数级规则在代码里声明（deny / escalate_to_l2），可单测覆盖。

审计：所有拒绝与 L1/L2 写操作都落 audit_logs（payload 脱敏，§13.12）。
"""

import logging
from typing import Any

from agent.tools.base import ToolError, ToolMeta

logger = logging.getLogger(__name__)


class PolicyDenied(ToolError):
    """策略拒绝（未注册 / 参数 deny / 风险升级后未接线）。"""


class ToolPolicy:
    """一条工具的本地策略：等级覆盖 + 参数规则。

    risk_level_override：不为 None 时**覆盖**工具 meta 自述的等级
    （本地注册表说了算，§13.10 第 1 条）。
    """

    def __init__(
        self,
        *,
        risk_level_override: int | None = None,
        deny_checks: list[Any] | None = None,
        escalate_checks: list[Any] | None = None,
        enabled: bool = True,
    ) -> None:
        self.risk_level_override = risk_level_override
        self.deny_checks = deny_checks or []
        self.escalate_checks = escalate_checks or []
        self.enabled = enabled


class PolicyRegistry:
    """工具名 → 本地策略。未注册 = 拒绝（fail-closed）。"""

    def __init__(self) -> None:
        self._policies: dict[str, ToolPolicy] = {}

    def register(self, name: str, policy: ToolPolicy | None = None) -> None:
        self._policies[name] = policy or ToolPolicy()

    def is_known(self, name: str) -> bool:
        return name in self._policies

    def check_enabled(self, name: str) -> None:
        p = self._policies.get(name)
        if p is None:
            # E7：未注册工具 → 拒绝 + 审计（由 ToolRegistry 落 audit_logs）
            raise PolicyDenied("TOOL_NOT_ALLOWED", f"工具 {name!r} 未在本地策略注册表登记，已拒绝（fail-closed）")
        if not p.enabled:
            raise PolicyDenied("TOOL_NOT_ALLOWED", f"工具 {name!r} 已被策略禁用")

    def effective_risk_level(self, meta: ToolMeta) -> int:
        p = self._policies.get(meta.name)
        if p and p.risk_level_override is not None:
            return p.risk_level_override
        return meta.risk_level

    def evaluate_args(self, name: str, args: Any) -> tuple[int, list[str]]:
        """参数级评估：返回 (生效风险等级, 说明列表)。

        规则命中 deny → PolicyDenied；命中 escalate → 等级升至 2（L2，
        M5 接审批前表现为「执行被拦截，等待审批能力」）。
        """
        p = self._policies.get(name)
        if p is None:
            raise PolicyDenied("TOOL_NOT_ALLOWED", f"工具 {name!r} 未注册")
        notes: list[str] = []
        for rule in p.deny_checks:
            hit, why = rule.check(args)
            if hit:
                raise PolicyDenied("TOOL_NOT_ALLOWED", f"参数被策略拒绝：{why}")
        risk = self.effective_risk_level_from_policy(p)
        for rule in p.escalate_checks:
            hit, why = rule.check(args)
            if hit:
                notes.append(f"参数触发风险升级：{why}")
                risk = max(risk, 2)
        return risk, notes

    def effective_risk_level_from_policy(self, p: ToolPolicy) -> int:
        return p.risk_level_override if p.risk_level_override is not None else 0

    def all_known(self) -> list[str]:
        return sorted(self._policies)


# ---------------- 全局单例与注册 ----------------

_policy_registry = PolicyRegistry()


def get_policy_registry() -> PolicyRegistry:
    return _policy_registry


def default_policies() -> dict[str, ToolPolicy]:
    """M4 内置工具的本地策略（风险等级以本地为准，不信任 meta 自述）。"""
    return {
        # 知识库检索：只读
        "retrieval": ToolPolicy(risk_level_override=0),
        # 工具结果全文取回：只读
        "fetch_full": ToolPolicy(risk_level_override=0),
        # 数学沙箱：本地子进程执行用户代码，无外发无持久化 → L0
        "sandbox": ToolPolicy(risk_level_override=0),
        # 待办写入：写操作 → L1（执行 + 事后告知）
        "todo": ToolPolicy(risk_level_override=1),
        "todo_list": ToolPolicy(risk_level_override=0),
        "todo_complete": ToolPolicy(risk_level_override=1),
        # web 搜索：外发查询词到第三方 → 本地定 L0（查询词非敏感；若含 PII 由
        # 上游 guard_input 负责脱敏，M9 落地）——显式声明而非默认值
        "search": ToolPolicy(risk_level_override=0),
    }


def bootstrap_policies() -> PolicyRegistry:
    """用默认策略填充全局注册表（应用启动与测试夹具调用）。"""
    for name, policy in default_policies().items():
        _policy_registry.register(name, policy)
    return _policy_registry
