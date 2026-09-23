"""审批端点（M5-2/M5-6，§7.3）：人工决策 + resume 投递。

POST /api/v1/approvals/{id}/decide   {approved: bool, note?: str}
    → 状态机 pending → approved/rejected（过期/已决 → 409）
    → approved：投递 resume 后台任务（Command(resume=决策) 从 checkpoint 续跑，
      事件写回原消息的 stream）—— kill -9 后依然成立（F3：状态在 DB/checkpoint）

GET  /api/v1/approvals/pending       前端审批卡片的数据源（过期惰性置 expired）
"""

import asyncio
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from apps.api.api.v1.chat_deps import get_llm_client
from apps.api.api.v1.chat_runtime import TurnSpec, run_turn_task
from apps.api.core.db import tenant_session
from apps.api.core.deps import UserCtx, current_user
from apps.api.core.errors import AppError, ErrorCode
from apps.api.core.locks import acquire_conversation_lock
from apps.api.core.redis import get_redis
from apps.api.repositories import approvals as approvals_repo
from apps.api.repositories import conversations as conv_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


class DecideRequest(BaseModel):
    approved: bool = Field(description="true=批准执行，false=拒绝")
    note: str | None = Field(default=None, max_length=500, description="决策备注（拒绝理由等）")


@router.get("/pending")
async def list_pending(user: Annotated[UserCtx, Depends(current_user)]) -> dict:
    async with tenant_session(user.id) as session:
        items = await approvals_repo.list_pending(session, user.id)
    return {"data": {"approvals": items}}


@router.post("/{approval_id}/decide")
async def decide(
    approval_id: UUID,
    req: DecideRequest,
    user: Annotated[UserCtx, Depends(current_user)],
    request: Request,
) -> dict:
    """决策并（批准时）投递 resume 任务。拒绝时只更新状态——
    resume 后模型收到「审批拒绝」observation，给出替代回答（F2）。"""
    redis = get_redis()
    trace_id = getattr(request.state, "trace_id", "") or __import__(
        "apps.api.core.errors", fromlist=["new_trace_id"]
    ).new_trace_id()

    async with tenant_session(user.id) as session:
        approval = await approvals_repo.decide_approval(
            session, user.id, approval_id, approved=req.approved
        )
        if approval is None:
            raise AppError(
                ErrorCode.CONFLICT, "审批不存在、已决策或已过期", 409,
                headers={"Retry-After": "1"},
            )

    if not req.approved:
        # 拒绝：不需要跑图。resume 由下一次同会话消息触发时带上（见下）
        # —— 但 F2 要求「Agent 收到拒绝信号并给出替代回答」，所以拒绝也要 resume：
        # Command(resume={approved:false}) 让 interrupt() 返回，react 把拒绝作为
        # Observation 回灌，模型继续生成替代回答。
        pass

    thread_id = approval["thread_id"]
    # 该 thread 的最新 assistant 消息（interrupted 状态）→ resume 事件写回它
    async with tenant_session(user.id) as session:
        message = await conv_repo.get_latest_assistant_interrupted(
            session, user.id, UUID(thread_id)
        )
    if message is None:
        raise AppError(ErrorCode.CONFLICT, "找不到待恢复的消息", 409)

    lock_token = await acquire_conversation_lock(redis, thread_id)
    if lock_token is None:
        raise AppError(ErrorCode.CONFLICT, "该会话正在处理中，请稍后再试", 409,
                       headers={"Retry-After": "5"})

    llm = await get_llm_client()
    from langgraph.types import Command

    spec = TurnSpec(
        llm=llm, user=user, conversation_id=UUID(thread_id),
        message_id=UUID(str(message["id"])),
        user_seq=int(message["seq"]) - 1,
        content="",
        trace_id=trace_id,
        resume_command=Command(
            resume={"approved": req.approved, "note": req.note or "", "decided_by": user.email}
        ),
        extra={"lock_token": lock_token, "approval_id": str(approval_id)},
    )
    asyncio.create_task(run_turn_task(spec))
    return {
        "data": {
            "ok": True,
            "approval_id": str(approval_id),
            "decision": approval["status"],
            "message_id": str(message["id"]),
            "stream_url": f"/api/v1/chat/{message['id']}/stream",
        },
        "trace_id": trace_id,
    }
