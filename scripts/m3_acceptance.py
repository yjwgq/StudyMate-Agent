"""M3 验收 D1–D5 执行脚本（可重复运行，输出即验收证据）。

用法：uv run python scripts/m3_acceptance.py
前置：栈已启动（http://127.0.0.1:8080）、语料已入库、.env 有 Langfuse key。
"""

import asyncio
import datetime as dt
import json
import re
import sys
import uuid
from pathlib import Path

import httpx

# 脚本可从任意 cwd 运行：把项目根加入 sys.path（agent/ 与 apps/ 的导入依赖）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8080"
PHONE = "13812345678"
EMAIL, PASSWORD = "d1_test@example.com", "D1Pass123!"


def parse_block(block: str):
    event, data_lines = "message", []
    for line in block.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
    if not data_lines:
        return None
    try:
        return {"event": event, "data": json.loads(data_lines[0])}
    except Exception:
        return None


async def chat_once(
    client: httpx.AsyncClient, content: str, key: str, headers: dict | None = None
) -> dict:
    """按前端语义处理事件流（notice clear 清空当前气泡）。

    headers 用于以**另一个身份**发起请求（D5 需要 B 的 token——
    初版脚本漏传导致 B 的提问实际是 A 发的，验收假失败）。

    key 会被拼上每次唯一的 uuid：**固定 key 会让第二次运行命中幂等重放**，
    重放流不发 citations、不产生新 trace，验收结果全是假的（M3 踩坑）。
    重放路径本身另有单测与 B7 覆盖。
    """
    ev: dict = {"text": "", "citations": [], "done": None, "notice": [], "degraded": []}
    send_headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": f"{key}-{uuid.uuid4().hex[:12]}",
    }
    send_headers.update(headers or {})
    r = await client.post(
        f"{BASE}/api/v1/chat",
        headers=send_headers,
        json={"content": content},
    )
    buf = ""
    async for chunk in r.aiter_bytes():
        buf += chunk.decode("utf-8", errors="replace")
        i = buf.find("\n\n")
        while i != -1:
            block, buf = buf[:i].strip(), buf[i + 2:]
            e = parse_block(block)
            if e:
                t, d = e["event"], e["data"]
                if t == "token" and d.get("delta"):
                    ev["text"] += d["delta"]
                elif t == "citations":
                    ev["citations"] = d.get("citations", [])
                elif t == "notice":
                    notices = ev["notice"]
                    if isinstance(notices, list):
                        notices.append(d)
                    if d.get("clear"):
                        ev["text"] = ""
                elif t == "done":
                    ev["done"] = d
                elif t == "degraded":
                    ev["degraded"] = d.get("degraded", [])
            i = buf.find("\n\n")
    return ev


def wait_for_trace(trace_id: str, timeout_s: int = 120, interval_s: int = 6) -> list[dict]:
    """轮询直到 Langfuse 摄入该 trace（云端有数十秒级延迟，固定 sleep 不可靠）。

    M3 验收踩坑：sleep(8) 后查询返回空，误判为"span 未上报"；
    实际数据几十秒后才可见。
    """
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        obs = langfuse_obs(trace_id, minutes=30)
        if obs:
            return obs
        time.sleep(interval_s)
    return []


def langfuse_obs(trace_id: str, minutes: int = 15) -> list[dict]:
    from dotenv import dotenv_values

    env = dotenv_values(".env")
    host = str(env["LANGFUSE_HOST"]).rstrip("/")
    pk, sk = str(env["LANGFUSE_PUBLIC_KEY"]), str(env["LANGFUSE_SECRET_KEY"])
    now = dt.datetime.now(dt.UTC)
    with httpx.Client(
        timeout=30, auth=(pk, sk)
    ) as lc:
        r = lc.get(
            f"{host}/api/public/v2/observations",
            params={
                "fromStartTime": (now - dt.timedelta(minutes=minutes)).isoformat(),
                "toStartTime": now.isoformat(),
                "limit": 300,
                "fields": "core,io,usage,model",
            },
        )
        return [o for o in r.json().get("data", []) if o.get("traceId") == trace_id]


async def main() -> int:
    from agent.guardrails.groundedness import check_groundedness

    fails: list[str] = []
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{BASE}/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
        if r.status_code != 200:
            print(f"登录失败 {r.status_code}: {r.text[:200]}")
            return 2
        c.headers["Authorization"] = f"Bearer {r.json()['data']['access_token']}"

        # ---------------- D1 ----------------
        print("=" * 70)
        print("D1: 提问 → 答案带 [n] 脚注，无出处事实句占比 < 20%")
        print("=" * 70)
        queries = [
            ("监督学习是什么意思？它和过拟合有什么关系？", "acc-d1-1"),
            ("请对比监督学习和无监督学习的区别。", "acc-d1-2"),
            ("数据库里的 B+ 树索引和 MVCC 分别解决什么问题？", "acc-d1-3"),
            ("RAG 里的 RRF 融合和重排序分别起什么作用？", "acc-d1-4"),
            ("统计学的中心极限定理说的是什么？", "acc-d1-5"),
        ]
        rows, trace_ids = [], []
        for q, key in queries:
            ev = await chat_once(c, q, key)
            st = check_groundedness(ev["text"]).as_dict()
            srv = (ev["done"] or {}).get("groundedness")
            rows.append((q, st, ev, srv))
            trace_ids.append((ev["done"] or {}).get("trace_id"))
            cites = len(re.findall(r"\[\d+\]", ev["text"]))
            print(
                f"  {'PASS' if st['ok'] else 'FAIL'} 无出处={st['unsupported_ratio']:.2f} "
                f"({st['cited_claims']}/{st['total_claims']}) 脚注标记={cites} "
                f"citations={len(ev['citations'])} 服务端ratio="
                f"{srv['unsupported_ratio'] if srv else None}  {q[:26]}"
            )
        ok_n = sum(1 for _, st, _, _ in rows if st["ok"])
        with_cite = sum(1 for _, _, ev, _ in rows if re.search(r"\[\d+\]", ev["text"]))
        srv_n = sum(1 for _, _, _, srv in rows if srv is not None)
        d1 = ok_n == len(rows) and with_cite == len(rows)
        print(
            f"\n  D1 = {'PASS' if d1 else 'PARTIAL'}：达标 {ok_n}/{len(rows)}，"
            f"带脚注 {with_cite}/{len(rows)}，服务端上报 {srv_n}/{len(rows)}"
        )
        if not d1:
            fails.append("D1")

        # ---------------- D2 ----------------
        print("\n" + "=" * 70)
        print("D2: 引用脚注可定位来源文档与段落（结构化 citations）")
        print("=" * 70)
        last = rows[-1][2]
        for cite in last["citations"][:3]:
            print(
                f"  [{cite['n']}] 《{cite['title']}》 段落={cite['page']} "
                f"doc={str(cite['document_id'])[:8]}… score={cite['score']:.4f}"
            )
            print(f"       摘要: {cite['snippet'][:76]}…")
        d2 = bool(last["citations"]) and all(
            all(k in ct and ct[k] is not None for k in ("n", "title", "document_id", "snippet", "score"))
            for ct in last["citations"]
        )
        print(f"  D2 = {'PASS' if d2 else 'FAIL'}（n/title/document_id/snippet/score 全非空）")
        if not d2:
            fails.append("D2")

        # ---------------- D3 ----------------
        print("\n" + "=" * 70)
        print("D3: Langfuse trace 完整（检索 span + LLM span，含 token 与耗时）")
        print("=" * 70)
        tid = str(trace_ids[-1] or "")
        obs = wait_for_trace(tid) if tid else []
        if not tid:
            print("  ⚠️ 未取到 trace_id")
        print(f"  trace_id={tid}  spans={len(obs)}")
        for o in obs:
            # v2 API 的 token 字段是 usageDetails（不是 usage）
            usage = o.get("usageDetails") or o.get("totalUsage")
            print(
                f"    type={o.get('type'):<11} latency={o.get('latency')} "
                f"model={o.get('model') or o.get('modelId')} "
                f"usage={json.dumps(usage, ensure_ascii=False) if usage else None} "
                f"userId={o.get('userId') or None}"
            )
        types = {o.get("type") for o in obs}
        has_lat = bool(obs) and all(o.get("latency") is not None for o in obs)
        has_usage = any(o.get("usageDetails") for o in obs)
        has_model = any(o.get("model") for o in obs)
        has_user = any(o.get("userId") for o in obs)
        d3 = "RETRIEVER" in types and "GENERATION" in types and has_lat and has_usage
        print(
            f"  检索 span={'RETRIEVER' in types} LLM span={'GENERATION' in types} "
            f"耗时齐全={has_lat} token 用量={has_usage} model={has_model} userId={has_user}"
        )
        print(f"  D3 = {'PASS' if d3 else 'PARTIAL'}"
              f"{'（token 用量为 D3 硬要求，缺失即 PARTIAL）' if not has_usage else ''}")
        if not d3:
            fails.append("D3")

        # ---------------- D4 ----------------
        print("\n" + "=" * 70)
        print("D4: Langfuse trace 全文搜不到测试手机号（PII redaction）")
        print("=" * 70)
        ev4 = await chat_once(c, f"帮我记下手机号 {PHONE}，并说明 RRF 融合是什么", "acc-d4")
        tid4 = str((ev4["done"] or {}).get("trace_id") or "")
        obs4 = wait_for_trace(tid4) if tid4 else []
        if not tid4:
            print("  ⚠️ 未取到 trace_id")
        payload = json.dumps(
            [{"i": o.get("input"), "o": o.get("output")} for o in obs4], ensure_ascii=False
        )
        leaked = PHONE in payload
        print(f"  trace_id={tid4}  spans={len(obs4)}  检查载荷 {len(payload)} 字符")
        if "REDACTED" in payload:
            i = payload.find("REDACTED")
            print(f"  脱敏证据: ...{payload[max(0, i - 60):i + 30]}...")
        print(f"  D4 = {'FAIL — 手机号泄露' if leaked else 'PASS'}")
        if leaked:
            fails.append("D4")

        # ---------------- D5 ----------------
        print("\n" + "=" * 70)
        print("D5: 跨租户隔离（B 的语料为空 → 检索不到任何 A 的内容）")
        print("=" * 70)
        r = await c.post(
            f"{BASE}/api/v1/auth/register",
            json={"email": "d5b_acc@example.com", "password": "D5Pass123!", "display_name": "用户B"},
        )
        r = await c.post(
            f"{BASE}/api/v1/auth/login",
            json={"email": "d5b_acc@example.com", "password": "D5Pass123!"},
        )
        bh = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
        r = await c.post(
            f"{BASE}/api/v1/kb/search", json={"query": "监督学习 中心极限定理 RRF"}, headers=bh
        )
        hits_b = r.json()["data"]["hits"]
        r = await c.get(f"{BASE}/api/v1/kb/documents", headers=bh)
        docs_b = r.json()["data"]["items"]
        # 以 B 的身份提问（必须带 B 的 token，否则验的是 A）
        evb = await chat_once(c, "什么是监督学习？", "acc-d5", headers=bh)
        print(f"  B 检索命中: {len(hits_b)}（期望 0）")
        print(f"  B 文档数: {len(docs_b)}（期望 0）")
        print(f"  B 提问 citations: {len(evb['citations'])}（期望 0，无资料可引）")
        d5 = len(hits_b) == 0 and len(docs_b) == 0 and len(evb["citations"]) == 0
        print(f"  D5 = {'PASS' if d5 else 'FAIL'}")
        if not d5:
            fails.append("D5")

    print("\n" + "=" * 70)
    print(f"结论：{'D1–D5 全部 PASS' if not fails else '未通过：' + ', '.join(fails)}")
    print("=" * 70)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
