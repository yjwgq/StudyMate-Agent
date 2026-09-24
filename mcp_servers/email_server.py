"""邮件 MCP server（M5-1，§7.3 参数级升级策略的演示载体）。

诚实声明：这是**模拟发件**——邮件写入 data/outbox/（进程本地文件），
不接真实 SMTP。作为 L1/L2 分级与 HITL 审批的演示足够真实：
    - 发给内部域（@test.local）→ L1：直接执行 + 事后告知；
    - 收件人含外部域 → 参数级升级 L2 → interrupt → 人工审批（M5-2）；
    - 单次收件人 > 5 → deny（群发走营销系统，不在聊天里做）。
"""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from mcp_servers.common import make_server, run

app = make_server(
    "email",
    "代表用户发送邮件。发给外部域名需要人工审批；群发超过 5 人会被拒绝。"
    "发送成功后可用 list_outbox 查看已发送邮件。",
)

OUTBOX_DIR = Path(os.environ.get("EMAIL_OUTBOX_DIR", "data/outbox"))


class SendEmailArgs(BaseModel):
    to: list[str] = Field(min_length=1, max_length=10, description="收件人邮箱列表")
    subject: str = Field(min_length=1, max_length=200, description="主题")
    body: str = Field(min_length=1, max_length=8000, description="正文")


class ListOutboxArgs(BaseModel):
    limit: int = Field(default=5, ge=1, le=20)


@app.tool()
async def send_email(to: list[str], subject: str, body: str) -> str:
    """发送邮件。内部域直接发送；外部域需先经人工审批（治理层处理）。"""
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    mail_id = f"{int(time.time() * 1000)}-{os.getpid()}"
    record = {
        "id": mail_id,
        "to": to,
        "subject": subject,
        "body": body[:2000],
        "sent_at": datetime.now(UTC).isoformat(),
        "note": "模拟发件：写入本地 outbox，未接真实 SMTP",
    }
    (OUTBOX_DIR / f"{mail_id}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    return json.dumps(
        {"ok": True, "mail_id": mail_id, "to": to, "subject": subject,
         "note": record["note"]},
        ensure_ascii=False,
    )


@app.tool()
async def list_outbox(limit: int = 5) -> str:
    """列出最近发送的邮件（模拟收发件箱）。"""
    mails: list[dict[str, Any]] = []
    if OUTBOX_DIR.exists():
        files = sorted(OUTBOX_DIR.glob("*.json"), reverse=True)[:limit]
        for f in files:
            try:
                mails.append(json.loads(f.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue
    return json.dumps({"ok": True, "mails": mails}, ensure_ascii=False)


if __name__ == "__main__":
    run(app)
