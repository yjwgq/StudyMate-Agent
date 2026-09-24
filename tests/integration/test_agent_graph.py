"""Agent 图集成测试（M4-12，验收 E1–E7）：fake LLM 驱动整张图。

不真调模型（§16：集成测试层快且不烧额度）；工具层用真实实现
（MCP sandbox/todo 为真子进程 + 真 DB），保证治理层行为可信。
"""

import time
import uuid

import pytest
from sqlalchemy import text

from agent.graph.builder import run_agent_turn
from agent.tools.base import ToolCtx
from tests.integration.scripted_tools import ScriptedLLM, plan_json, tool_call

pytestmark = pytest.mark.asyncio


async def _run_turn(registry, test_user, llm, *, content="任务", token_budget=30000):
    """跑一轮 agent，收集事件与 final 摘要。"""
    events: list[dict] = []
    final: dict = {}
    async for kind, item in run_agent_turn(
        llm=llm,
        registry=registry,
        tool_ctx=ToolCtx(user_id=test_user, conversation_id=f"conv-{uuid.uuid4().hex[:8]}",
                         dedupe_seed="seed", extra={"tool_events": []}),
        user_id=test_user,
        conversation_id=f"conv-{uuid.uuid4().hex[:8]}",
        content=content,
        token_budget=token_budget,
    ):
        if kind == "event":
            events.append(item)
        else:
            final = item
    return events, final


# ---------------- E1：DAG 并行执行 ----------------


async def test_e1_parallel_steps_overlap(registry, test_user):
    """调研类问题 → planner 多 step，独立 step 并行执行（时间戳重叠 + 墙钟优于串行）。"""
    plan = plan_json([
        {"id": 1, "description": "调研主题A：slow_probe", "tool_hint": None, "depends_on": []},
        {"id": 2, "description": "调研主题B：slow_probe", "tool_hint": None, "depends_on": []},
        {"id": 3, "description": "汇总对比A与B", "tool_hint": None, "depends_on": [1, 2]},
    ])
    llm = ScriptedLLM(
        plan=plan,
        scripts={
            "调研主题A": [tool_call("t1", "slow_probe", {}), "[步骤1] 主题A结论"],
            "调研主题B": [tool_call("t2", "slow_probe", {}), "[步骤2] 主题B结论"],
            "汇总对比": ["[步骤3] A 与 B 的对比结论"],
        },
        final="A 与 B 的区别如下……",
    )
    started = time.perf_counter()
    events, final = await _run_turn(registry, test_user, llm)
    wall = time.perf_counter() - started

    # planner 产出 3 步
    assert len(final["plan"]) == 3
    # 两个独立 step 并行执行：时间区间重叠
    by_id = {s["id"]: s for s in final["plan"]}
    s1, s2 = by_id[1], by_id[2]
    assert s1["status"] == "done" and s2["status"] == "done"
    overlap = s1["started_at"] < s2["finished_at"] and s2["started_at"] < s1["finished_at"]
    assert overlap, f"steps 未并行：{s1['started_at']:.2f}-{s1['finished_at']:.2f} vs {s2['started_at']:.2f}-{s2['finished_at']:.2f}"
    # 墙钟证据：两个 0.8s 工具 + 多轮 LLM，并行执行下总耗时远小于串行 2×0.8s
    # （wave 级 gather；串行执行至少 1.6s）
    assert wall < 1.6, f"疑似串行执行：wall={wall:.2f}s"


# ---------------- E2：todo 工具落库 ----------------


async def test_e2_todo_tool_creates_row(registry, test_user, worker_engine):
    """「加一条待办」→ todo MCP 工具 → todos 表出现该行（dedupe_key 注入）。"""
    plan = plan_json([
        {"id": 1, "description": "创建待办：周五交作业", "tool_hint": "todo", "depends_on": []},
    ])
    llm = ScriptedLLM(
        plan=plan,
        scripts={
            "创建待办": [
                tool_call("t1", "todo", {"title": "周五交作业", "detail": "机器学习课程作业"}),
                "已创建待办「周五交作业」。",
            ],
        },
        final="好的，已为你创建待办。",
    )
    events, final = await _run_turn(registry, test_user, llm)

    assert final["plan"][0]["status"] == "done"
    async with worker_engine.begin() as conn:
        row = await conn.execute(
            text("""
                SELECT title, user_id, dedupe_key FROM todos
                WHERE user_id = :uid AND title = :t
            """),
            {"uid": test_user, "t": "周五交作业"},
        )
        found = row.mappings().first()
    assert found is not None, "todo 未落库"
    assert found["dedupe_key"]  # 治理层注入的幂等键


# ---------------- E3：sandbox 计算 ----------------


async def test_e3_sandbox_computes(registry, test_user):
    """计算 1234 × 5678 → sandbox MCP 工具（RestrictedPython 子进程）返回正确结果。"""
    plan = plan_json([
        {"id": 1, "description": "计算 1234 乘以 5678", "tool_hint": "sandbox", "depends_on": []},
    ])
    llm = ScriptedLLM(
        plan=plan,
        scripts={
            "计算 1234": [
                tool_call("t1", "sandbox", {"code": "_ = 1234 * 5678"}),
                "[步骤1] 计算完成：7006652",
            ],
        },
        final="1234 × 5678 = 7006652",
    )
    events, final = await _run_turn(registry, test_user, llm)
    assert final["plan"][0]["status"] == "done"
    assert "7006652" in final["plan"][0]["result"]


# ---------------- E4：超长工具结果截断 ----------------


async def test_e4_tool_result_truncated_with_full_ref(registry, test_user, worker_engine):
    """超长工具结果 → 截断 + full_ref，上下文不爆。"""
    plan = plan_json([
        {"id": 1, "description": "拉取超大结果 huge_probe", "tool_hint": None, "depends_on": []},
    ])
    llm = ScriptedLLM(
        plan=plan,
        scripts={
            "拉取超大结果": [
                tool_call("t1", "huge_probe", {}),
                "[步骤1] 已处理截断结果。",
            ],
        },
        final="结果已截断处理。",
    )
    events, final = await _run_turn(registry, test_user, llm)

    # react 收到的 observation 是截断后的（带 full_ref 提示）。
    # 注意：第 1 轮 complete 的消息里还没有 tool 结果 —— 覆盖全部轮次。
    react_msgs = [c for c in llm.complete_calls if c["span"] == "react"]
    tool_msgs = [
        m for c in react_msgs for m in c["messages"] if m.get("role") == "tool"
    ]
    assert any("full_ref=" in str(m.get("content", "")) for m in tool_msgs), "Observation 未截断"
    assert all(len(str(m.get("content", ""))) < 6000 for m in tool_msgs), "上下文被超长结果撑爆"

    # full_ref 文件确实存在且含全文
    from pathlib import Path

    refs = [
        str(m["content"]).split("full_ref=")[1].split("，")[0].strip()
        for m in tool_msgs if "full_ref=" in str(m.get("content", ""))
    ]
    assert refs, "未生成 full_ref"
    for ref in refs:
        p = Path("data/tool_results") / ref
        assert p.exists()
        assert len(p.read_text(encoding="utf-8")) == 20000

    # E6 附带断言：埋点记录了 truncated 标记
    async with worker_engine.begin() as conn:
        row = await conn.execute(
            text("""
                SELECT truncated, result_chars FROM tool_invocations
                WHERE user_id = :uid AND tool_name = 'huge_probe'
                ORDER BY created_at DESC LIMIT 1
            """),
            {"uid": test_user},
        )
        inv = row.mappings().first()
    assert inv is not None and inv["truncated"] is True
    assert inv["result_chars"] >= 4000


# ---------------- E5：循环检测 ----------------


async def test_e5_loop_detection_terminates(registry, test_user):
    """同一工具+相同参数反复调用 → 循环检测终止，不无限循环。"""
    plan = plan_json([
        {"id": 1, "description": "反复探测循环任务", "tool_hint": None, "depends_on": []},
    ])
    llm = ScriptedLLM(
        plan=plan,
        scripts={
            "反复探测循环任务": [
                tool_call("t1", "slow_probe", {}),   # round1 正常
                tool_call("t2", "slow_probe", {}),   # round2 重复（相同 tool+args）
                tool_call("t3", "slow_probe", {}),   # round3 仍重复 → 脚本耗尽兜底 final
            ],
        },
        final="检测到循环，终止。",
    )
    events, final = await _run_turn(registry, test_user, llm)

    step = final["plan"][0]
    # step 终止（不无限循环）：完成的 step 或带 error 的失败 step 都算终止
    assert step["status"] in ("done", "failed")
    # round 数有限：脚本只有 3 轮 + 兜底
    assert step["attempts"] <= 2


# ---------------- E6：tool_invocations 埋点 ----------------


async def test_e6_tool_invocations_recorded(registry, test_user, worker_engine):
    """每次工具调用都有埋点：名称/耗时/成功失败/是否截断。"""
    plan = plan_json([
        {"id": 1, "description": "探测埋点 slow_probe", "tool_hint": None, "depends_on": []},
        {"id": 2, "description": "触发失败 unknown", "tool_hint": None, "depends_on": []},
    ])
    llm = ScriptedLLM(
        plan=plan,
        scripts={
            "探测埋点": [tool_call("t1", "slow_probe", {}), "步骤1完成"],
            "触发失败": [
                tool_call("t2", "retrieval", {"query": "x" * 10, "top_k": 1}),  # 合法调用（空库 ok=True）
                "步骤2结束",
            ],
        },
        final="完成。",
    )
    events, final = await _run_turn(registry, test_user, llm)

    async with worker_engine.begin() as conn:
        rows = await conn.execute(
            text("""
                SELECT tool_name, result_ok, latency_ms, truncated, deduplicated
                FROM tool_invocations WHERE user_id = :uid
                ORDER BY created_at ASC
            """),
            {"uid": test_user},
        )
        invs = [dict(r) for r in rows.mappings().all()]
    names = [i["tool_name"] for i in invs]
    assert "slow_probe" in names
    slow = next(i for i in invs if i["tool_name"] == "slow_probe")
    assert slow["result_ok"] is True
    assert slow["latency_ms"] >= 700   # 0.8s 工具的耗时被记录


# ---------------- E7：未注册工具 fail-closed + 审计 ----------------


async def test_e7_unregistered_tool_denied_and_audited(registry, test_user, worker_engine):
    """调用未注册工具 → policy 拒绝（observation 回灌），audit_logs 有 tool.denied。"""
    plan = plan_json([
        {"id": 1, "description": "删除文件任务", "tool_hint": None, "depends_on": []},
    ])
    llm = ScriptedLLM(
        plan=plan,
        scripts={
            "删除文件任务": [
                tool_call("t1", "filesystem_delete", {"path": "/etc/passwd"}),  # 未注册
                "[步骤1] 工具被拒绝，未执行任何删除。",
            ],
        },
        final="该工具不可用。",
    )
    events, final = await _run_turn(registry, test_user, llm)

    # 模型收到的是「拒绝」observation，而不是执行结果
    react_msgs = [c for c in llm.complete_calls if c["span"] == "react"]
    tool_msgs = [
        m for c in react_msgs for m in c["messages"] if m.get("role") == "tool"
    ]
    assert any("未在本地策略注册表登记" in str(m.get("content", "")) for m in tool_msgs)

    # 审计落库（fail-closed 的证据链）
    async with worker_engine.begin() as conn:
        row = await conn.execute(
            text("""
                SELECT action, target FROM audit_logs
                WHERE user_id = :uid AND action = 'tool.denied'
                ORDER BY created_at DESC LIMIT 1
            """),
            {"uid": test_user},
        )
        audit = row.mappings().first()
    assert audit is not None
    assert audit["target"] == "filesystem_delete"

    # 没有任何东西被删除：埋点里该工具 result_ok=false
    async with worker_engine.begin() as conn:
        row = await conn.execute(
            text("""
                SELECT result_ok, error_code FROM tool_invocations
                WHERE user_id = :uid AND tool_name = 'filesystem_delete'
                ORDER BY created_at DESC LIMIT 1
            """),
            {"uid": test_user},
        )
        inv = row.mappings().first()
    assert inv is not None and inv["result_ok"] is False
    assert inv["error_code"] == "TOOL_NOT_ALLOWED"
