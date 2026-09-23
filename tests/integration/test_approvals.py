"""HITL 审批集成测试（M5-2，F1/F2 预演）：fake LLM 驱动真实 LangGraph interrupt/resume。

流程覆盖：
    L2 触发（参数级升级：外部域收件人）→ approvals 落 pending → 图暂停
    → 批准：Command(resume) → 工具执行 + approvals.executed + outbox 文件（F1）
    → 拒绝：Command(resume) → Observation 回灌 → 替代回答 + approvals.rejected（F2）
"""

import json
import uuid
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from agent.graph.builder import assemble_graph
from agent.tools.base import ToolCtx
from tests.integration.scripted_tools import ScriptedLLM, plan_json, tool_call

pytestmark = pytest.mark.asyncio


async def _start_turn(registry, test_user):
    """发起一轮会触发 L2 审批的任务，返回 (graph, config, 最终 values)。"""
    from langgraph.checkpoint.memory import MemorySaver

    plan = plan_json([
        {"id": 1, "description": "发送邮件给外部合作伙伴", "tool_hint": "send_email", "depends_on": []},
    ])
    llm = ScriptedLLM(
        plan=plan,
        scripts={
            "发送邮件": [
                tool_call(
                    "t1", "send_email",
                    {"to": ["partner@corp.com"], "subject": "合作沟通", "body": "您好，想约个时间聊聊。"},
                ),
                "[步骤1] 邮件已处理。",
            ],
        },
        final="邮件事项已按您的决定处理完毕。",
    )
    checkpointer = MemorySaver()  # 单测用内存 checkpoint；F3 的 Postgres 恢复在真实端点验收
    graph = assemble_graph(checkpointer=checkpointer)
    cid = f"conv-{uuid.uuid4().hex[:8]}"
    config = {
        "configurable": {
            "thread_id": cid,
            "llm": llm,
            "registry": registry,
            "tool_ctx": ToolCtx(
                user_id=test_user, conversation_id=cid,
                dedupe_seed=cid, extra={"tool_events": []},
            ),
            "token_budget": 30000,
        }
    }
    graph_input = {
        "messages": [{"role": "user", "content": "给外部合作伙伴发邮件约会议"}],
        "user_id": test_user,
        "conversation_id": cid,
        "trace_id": uuid.uuid4().hex,
        "mode": "auto",
        "max_parallel": 3,
        "token_budget": 30000,
        "degraded": [],
    }
    return graph, config, graph_input, llm


async def test_f1_approve_executes_tool(registry, test_user, worker_engine):
    """批准 → 工具执行 → approvals.executed → outbox 文件存在。"""
    graph, config, graph_input, llm = await _start_turn(registry, test_user)

    # 第一段：跑到 interrupt 暂停
    final_state = {}
    async for _mode, chunk in graph.astream(graph_input, config, stream_mode=["custom", "values"]):
        if _mode == "values":
            final_state = chunk or {}
    interrupts = final_state.get("__interrupt__")
    assert interrupts, "图未被 L2 审批暂停"

    intr = list(interrupts)[0]
    value = getattr(intr, "value", intr)
    assert value["type"] == "approval"
    assert value["tool"] == "send_email"
    assert "外部域" in value["reason"]  # 参数级升级（§7.5 原例）
    approval_id = value["approval_id"]

    # approvals 表有 pending 行 + 审计
    async with worker_engine.begin() as conn:
        row = await conn.execute(
            text("SELECT status, tool_call FROM approvals WHERE id = CAST(:aid AS UUID)"),
            {"aid": approval_id},
        )
        approval = row.mappings().first()
    assert approval is not None and approval["status"] == "pending"
    assert approval["tool_call"]["tool"] == "send_email"

    # 决策（等价于 POST /approvals/{id}/decide）：先落 approved 再 resume ——
    # registry 的 resume 路径会校验「必须 approved」，跳过这步就是伪造决策
    from langgraph.types import Command
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from apps.api.repositories import approvals as approvals_repo

    maker = async_sessionmaker(worker_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        decided = await approvals_repo.decide_approval(
            session, UUID(test_user), UUID(approval_id), approved=True
        )
        assert decided is not None and decided["status"] == "approved"

    resumed_state = {}
    async for _mode, chunk in graph.astream(
        Command(resume={"approved": True, "note": "ok", "decided_by": "t"}), config,
        stream_mode=["custom", "values"],
    ):
        if _mode == "values":
            resumed_state = chunk or {}

    # 工具真实执行：outbox 文件 + approvals.executed
    outbox = Path("data/outbox")
    mails = list(outbox.glob("*.json")) if outbox.exists() else []
    assert any(
        json.loads(p.read_text(encoding="utf-8")).get("to") == ["partner@corp.com"]
        for p in mails
    ), "邮件未写入 outbox"
    async with worker_engine.begin() as conn:
        row = await conn.execute(
            text("SELECT status FROM approvals WHERE id = CAST(:aid AS UUID)"),
            {"aid": approval_id},
        )
        assert row.scalar_one() == "executed"
    # 模型收到「审批通过」observation 并给出结论
    assert resumed_state.get("answer")


async def test_f2_reject_gives_alternative(registry, test_user, worker_engine):
    """拒绝 → 模型收到拒绝信号给出替代回答 → approvals.rejected，工具未执行。"""
    graph, config, graph_input, llm = await _start_turn(registry, test_user)

    final_state = {}
    async for _mode, chunk in graph.astream(graph_input, config, stream_mode=["custom", "values"]):
        if _mode == "values":
            final_state = chunk or {}
    intr = list(final_state["__interrupt__"])[0]
    approval_id = getattr(intr, "value", intr)["approval_id"]

    # 决策为拒绝（等价于 decide API）
    from langgraph.types import Command
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from apps.api.repositories import approvals as approvals_repo

    maker = async_sessionmaker(worker_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        decided = await approvals_repo.decide_approval(
            session, UUID(test_user), UUID(approval_id), approved=False
        )
        assert decided is not None and decided["status"] == "rejected"

    outbox_before = set(Path("data/outbox").glob("*.json")) if Path("data/outbox").exists() else set()
    resumed_state = {}
    async for _mode, chunk in graph.astream(
        Command(resume={"approved": False, "note": "外部邮件需走正式渠道", "decided_by": "t"}), config,
        stream_mode=["custom", "values"],
    ):
        if _mode == "values":
            resumed_state = chunk or {}

    # approvals.rejected；没有新邮件发出
    async with worker_engine.begin() as conn:
        row = await conn.execute(
            text("SELECT status FROM approvals WHERE id = CAST(:aid AS UUID)"),
            {"aid": approval_id},
        )
        assert row.scalar_one() == "rejected"
    outbox_after = set(Path("data/outbox").glob("*.json")) if Path("data/outbox").exists() else set()
    assert outbox_after == outbox_before, "拒绝后仍有邮件发出"

    # 图正常收尾：替代回答存在（不卡死 —— F2 的核心）
    assert resumed_state.get("answer")
