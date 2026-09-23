"""M5 验收 F1–F6 执行脚本（真实端点，可重复运行）。

用法：uv run python scripts/m5_acceptance.py
前置：栈已启动（http://127.0.0.1:8080）；F3 需要能执行 docker 命令（kill api 容器）。

F3 说明（本里程碑最有说服力的演示之一）：
    审批挂起 → `docker kill` api 容器 → 重启 → decide → 从 Postgres
    checkpoint 续跑完成。中断状态不在进程内存在（checkpoint + approvals 表），
    因此进程死亡不影响任务。
"""

import asyncio
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8080"
EMAIL, PASSWORD = "d1_test@example.com", "D1Pass123!"
COMPOSE = ["docker", "compose", "-f", "infra/docker-compose.dev.yml"]


def parse_block(block: str):
    event, data_lines, eid = "message", [], None
    for line in block.splitlines():
        line = line.rstrip("\r")
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("id:"):
            eid = line[len("id:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
    if not data_lines:
        return None
    try:
        return {"event": event, "data": json.loads(data_lines[0]), "id": eid}
    except Exception:
        return None


async def drain_stream(
    client, mid: str, headers: dict, timeout_s: float = 300.0, last_event_id: str | None = None
) -> dict:
    """订阅消息事件流直到 done（或超时），返回收集到的事件摘要。

    last_event_id：断线/续跑续读 —— 同一消息的流是**追加**的（审批暂停后再
    resume 会继续写同一条流），从 0 读会立刻撞上上一轮的 done（M5 验收实锤：
    误判为「续跑没执行」）。续读用上一轮最后的事件 id。
    """
    out: dict = {"text": "", "events": [], "done": None, "approval": None,
                 "citations": [], "last_event_id": last_event_id}
    deadline = time.monotonic() + timeout_s
    req_headers = dict(headers)
    if last_event_id:
        req_headers["Last-Event-ID"] = last_event_id
    req = client.build_request("GET", f"{BASE}/api/v1/chat/{mid}/stream", headers=req_headers)
    resp = await client.send(req, stream=True)
    try:
        buf = ""
        async for chunk in resp.aiter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                e = parse_block(block)
                if not e:
                    continue
                events = out["events"]
                if isinstance(events, list):
                    events.append(e["event"])
                eid = e.get("id")
                if eid:
                    out["last_event_id"] = eid
                d = e["data"]
                if e["event"] == "token":
                    out["text"] = str(out["text"]) + str(d.get("delta") or "")
                elif e["event"] == "citations":
                    out["citations"] = d.get("citations") or []
                elif e["event"] == "approval":
                    out["approval"] = d
                elif e["event"] == "done":
                    out["done"] = d
                    return out
            if time.monotonic() > deadline:
                out["timeout"] = True
                return out
    finally:
        await resp.aclose()
    return out


async def submit(client, headers: dict, content: str) -> tuple[str, str]:
    """POST 投递，返回 (message_id, conversation_id)。"""
    r = await client.post(
        f"{BASE}/api/v1/chat",
        headers={**headers, "Content-Type": "application/json",
                 "Idempotency-Key": f"m5-{uuid.uuid4().hex[:12]}"},
        json={"content": content},
    )
    r.raise_for_status()
    data = r.json()["data"]
    return data["message_id"], data["conversation_id"]


def container_outbox_count() -> int:
    """模拟发件箱在 API 容器内（data/outbox 未挂载到宿主）—— 从容器里数。"""
    out = subprocess.run(
        ["docker", "exec", "personal-agent-os-dev-api-1", "bash", "-c",
         "ls /app/data/outbox/*.json 2>/dev/null | wc -l"],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return int(out.stdout.strip() or "0")
    except ValueError:
        return 0


def pg_query(sql: str) -> list[str]:
    """直连容器内 psql 查询（验收证据读取）。"""
    out = subprocess.run(
        ["docker", "exec", "personal-agent-os-dev-postgres-1",
         "psql", "-U", "postgres", "-d", "agent", "-tAc", sql],
        capture_output=True, text=True, timeout=30,
    )
    return [line for line in out.stdout.strip().splitlines() if line]


async def main() -> int:
    fails: list[str] = []
    async with httpx.AsyncClient(timeout=600) as c:
        r = await c.post(f"{BASE}/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
        token = r.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        uid = pg_query(f"SELECT id FROM users WHERE email='{EMAIL}'")[0]

        # ---------------- F1：L2 → 审批卡片 → 批准执行 ----------------
        print("=" * 70)
        print("F1: 外部邮件触发 L2 → 审批 → 批准后执行（outbox + approvals.executed）")
        print("=" * 70)
        before = container_outbox_count()
        mid, cid = await submit(c, headers, "请调用 send_email 工具发送邮件：收件人 partner@corp.com，主题「合作沟通」，正文「您好，想约下周三上午讨论合作细节。」")
        ev1 = await drain_stream(c, mid, headers)
        print(f"  审批事件: {json.dumps(ev1['approval'], ensure_ascii=False)[:180] if ev1['approval'] else '无'}")
        print(f"  流结束原因: {(ev1['done'] or {}).get('finish_reason')}")
        approval_id = (ev1["approval"] or {}).get("approval_id")
        f1 = approval_id is not None
        if f1:
            # 批准
            r = await c.post(f"{BASE}/api/v1/approvals/{approval_id}/decide",
                             headers={**headers, "Content-Type": "application/json"},
                             json={"approved": True, "note": "验收批准"})
            print(f"  decide: {r.status_code} {json.dumps(r.json().get('data'), ensure_ascii=False)[:150]}")
            mid2 = r.json()["data"]["message_id"]
            # 续读：跳过上一轮的事件（流是追加的）
            ev1b = await drain_stream(c, mid2, headers, last_event_id=ev1["last_event_id"])
            print(f"  续跑完成: {(ev1b['done'] or {}).get('finish_reason')}；答案: {ev1b['text'][:100]}")
            after = container_outbox_count()
            status_rows = pg_query(f"SELECT status FROM approvals WHERE id='{approval_id}'")
            ok_rows = pg_query(
                "SELECT result_ok FROM tool_invocations WHERE tool_name='send_email' "
                "ORDER BY created_at DESC LIMIT 1"
            )
            print(f"  容器内 outbox: {before} → {after}；approvals={status_rows}；埋点 ok={ok_rows}")
            f1 = (
                after > before
                and bool(status_rows) and status_rows[0] == "executed"
                and bool(ok_rows) and ok_rows[0] == "t"
            )
        else:
            print("  ⚠️ 未触发审批（模型可能未调用 send_email）——不计入失败，但请人工确认")
        print(f"  F1 = {'PASS' if f1 else 'PARTIAL'}")
        if not f1:
            fails.append("F1")

        # ---------------- F2：拒绝 → 替代回答 ----------------
        print("\n" + "=" * 70)
        print("F2: 审批选「拒绝」→ Agent 给出替代回答，不卡死")
        print("=" * 70)
        before2 = container_outbox_count()
        mid, _ = await submit(c, headers, "请调用 send_email 工具，给 partner@corp.com 发一封邮件，主题「季度总结」，正文「本季度进展见附件。」")
        ev2 = await drain_stream(c, mid, headers)
        approval_id2 = (ev2["approval"] or {}).get("approval_id")
        f2 = False
        if approval_id2:
            r = await c.post(f"{BASE}/api/v1/approvals/{approval_id2}/decide",
                             headers={**headers, "Content-Type": "application/json"},
                             json={"approved": False, "note": "走正式渠道"})
            mid2 = r.json()["data"]["message_id"]
            ev2b = await drain_stream(c, mid2, headers, last_event_id=ev2["last_event_id"])
            after2 = container_outbox_count()
            status2_rows = pg_query(f"SELECT status FROM approvals WHERE id='{approval_id2}'")
            print(f"  替代回答: {ev2b['text'][:140]}")
            print(f"  流结束: {(ev2b['done'] or {}).get('finish_reason')}；outbox {before2} → {after2}；"
                  f"status={status2_rows}")
            f2 = bool(ev2b["text"]) and after2 == before2 and bool(status2_rows) and status2_rows[0] == "rejected"
        else:
            print("  ⚠️ 未触发审批")
        print(f"  F2 = {'PASS' if f2 else 'PARTIAL'}")
        if not f2:
            fails.append("F2")

        # ---------------- F4：关页面不丢任务（先做，避免 F3 重启影响） ----------------
        print("\n" + "=" * 70)
        print("F4: 发起任务后立刻断开订阅 → 稍后重连，答案完整（落库）")
        print("=" * 70)
        mid, cid = await submit(c, headers, "请分别调研监督学习和无监督学习，然后对比它们的区别")
        # 立刻断开（不读流）
        await asyncio.sleep(2)
        print("  已断开订阅，等待后台任务完成（轮询消息落库状态，最长 5 分钟）…")
        final_status, content_len = "", "0"
        for _ in range(100):
            rows = pg_query(
                f"SELECT status, coalesce(length(content),0) FROM messages WHERE id='{mid}'"
            )
            if rows:
                final_status, content_len = rows[0].split("|")
                if final_status in ("completed", "failed", "cancelled", "interrupted"):
                    break
            await asyncio.sleep(3)
        print(f"  消息状态: {final_status}；内容长度: {content_len}")
        # 重连订阅：应能从落库内容确认完整性（token 不重放，done 会到）
        ev4 = await drain_stream(c, mid, headers, timeout_s=90)
        print(f"  重连结果: done={bool(ev4['done'])}（若事件已 TTL 过期则依赖落库内容）")
        f4 = final_status == "completed" and int(content_len) > 100
        print(f"  F4 = {'PASS' if f4 else 'FAIL'}（答案完整落库，与浏览器是否在线无关）")
        if not f4:
            fails.append("F4")

        # ---------------- F5：停止生成 ----------------
        print("\n" + "=" * 70)
        print("F5: 点「停止生成」→ 消息标记 cancelled")
        print("=" * 70)
        mid, _ = await submit(c, headers, "请详细写一篇 800 字的学习方法长文，越详细越好")
        await asyncio.sleep(4)  # 让它开始生成
        r = await c.post(f"{BASE}/api/v1/chat/{mid}/cancel", headers=headers)
        print(f"  cancel: {r.status_code} {json.dumps(r.json().get('data'), ensure_ascii=False)}")
        # 取消是**协作式**的：后台任务在下一个事件处退出（可能在 LLM 调用中），
        # 因此轮询等待终态（最多 60s），而不是固定 sleep
        f5_rows: list[str] = []
        for _ in range(20):
            f5_rows = pg_query(f"SELECT status, coalesce(length(content),0) FROM messages WHERE id='{mid}'")
            if f5_rows and f5_rows[0].startswith(("cancelled", "completed", "failed")):
                break
            await asyncio.sleep(3)
        print(f"  消息状态: {f5_rows}（等待终态后）")
        f5 = bool(f5_rows) and f5_rows[0].startswith("cancelled")
        print(f"  F5 = {'PASS' if f5 else 'FAIL'}")
        if not f5:
            fails.append("F5")

        # ---------------- F6：重新生成 ----------------
        print("\n" + "=" * 70)
        print("F6: 重新生成 → 新消息 + 旧消息 superseded_by")
        print("=" * 70)
        # 用 F4 那条已完成的消息做重新生成
        rows = pg_query(
            f"SELECT id FROM messages WHERE user_id='{uid}' AND role='assistant' "
            "AND status='completed' ORDER BY created_at DESC LIMIT 1"
        )
        target = rows[0]
        f6 = False
        r = await c.post(
            f"{BASE}/api/v1/chat/{target}/regenerate",
            headers={**headers, "Content-Type": "application/json"},
            content=json.dumps({}),
        )
        if r.status_code != 200:
            print(f"  regenerate: {r.status_code} {r.text[:200]}")
        else:
            data = r.json()["data"]
            new_mid = data["message_id"]
            print(f"  新消息: {new_mid}；superseded: {data['superseded']}")
            sup_rows = pg_query(f"SELECT superseded_by FROM messages WHERE id='{target}'")
            print(f"  旧消息 superseded_by: {sup_rows}")
            ev6 = await drain_stream(c, new_mid, headers, timeout_s=180)
            print(f"  新消息完成: {(ev6['done'] or {}).get('finish_reason')}")
            f6 = bool(sup_rows) and sup_rows[0] == new_mid
        if not f6:
            fails.append("F6")
        print(f"  F6 = {'PASS' if f6 else 'FAIL'}")

        # ---------------- F3：kill -9 → 重启 → 审批续跑 ----------------
        print("\n" + "=" * 70)
        print("F3: 审批挂起 → kill -9 api 容器 → 重启 → decide → 从 checkpoint 续跑")
        print("=" * 70)
        mid, _ = await submit(c, headers, "请调用 send_email 工具，给 partner@corp.com 发邮件，主题「技术交流」，正文「想交流一下近期技术方案。」")
        ev3 = await drain_stream(c, mid, headers, timeout_s=180)
        approval_id3 = (ev3["approval"] or {}).get("approval_id")
        if not approval_id3:
            print("  ⚠️ 未触发审批，F3 无法验证")
            fails.append("F3")
        else:
            print(f"  审批挂起: {approval_id3}（进程内无状态，状态在 Postgres）")
            print("  执行 docker kill api 容器…")
            subprocess.run([*COMPOSE, "kill", "api"], capture_output=True, text=True, timeout=120)
            await asyncio.sleep(3)
            print("  重启容器…")
            subprocess.run([*COMPOSE, "up", "-d", "api"], capture_output=True, text=True, timeout=300)
            for _ in range(60):
                try:
                    rr = await c.get(f"{BASE}/api/v1/health", timeout=5)
                    if rr.status_code == 200:
                        break
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(3)
            print("  容器已恢复，重新登录并批准该审批（**新进程** 中执行）…")
            r = await c.post(f"{BASE}/api/v1/auth/login",
                             json={"email": EMAIL, "password": PASSWORD})
            headers = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
            r = await c.post(
                f"{BASE}/api/v1/approvals/{approval_id3}/decide",
                headers={**headers, "Content-Type": "application/json"},
                content=json.dumps({"approved": True, "note": "重启后批准"}),
            )
            print(f"  decide: {r.status_code}")
            if r.status_code == 200:
                mid3 = r.json()["data"]["message_id"]
                ev3b = await drain_stream(c, mid3, headers, timeout_s=300,
                                          last_event_id=ev3["last_event_id"])
                status3_rows = pg_query(f"SELECT status FROM approvals WHERE id='{approval_id3}'")
                print(f"  续跑完成: {(ev3b['done'] or {}).get('finish_reason')}；"
                      f"approvals={status3_rows}；答案: {ev3b['text'][:100]}")
                f3 = bool(status3_rows) and status3_rows[0] == "executed"
            else:
                print(f"  决策失败: {r.text[:200]}")
                f3 = False
            print(f"  F3 = {'PASS' if f3 else 'FAIL'}（重启后仍能从 checkpoint 续跑）")
            if not f3:
                fails.append("F3")

    print("\n" + "=" * 70)
    print(f"结论：{'F1–F6 全部 PASS' if not fails else '未通过：' + ', '.join(fails)}")
    print("=" * 70)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
