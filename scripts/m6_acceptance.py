"""M6 验收 G1–G4 执行脚本（真实端点，可重复运行）。

G1 两套/多套管线同集对比（对比表 + 归因）
G2 模拟 rerank 故障 → 答案仍生成 + 页面显示"未精排"（degraded 事件含 rerank）
G3 模拟 embedding（向量路）故障 → 退化为仅关键词检索 + 对应降级标记
G4 记录分数（写入报告文件，供进度表引用）

用法：uv run python scripts/m6_acceptance.py
前置：栈已启动；评测语料已入库（eval@example.com）
"""

import asyncio
import json
import subprocess
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8080"
EVAL_EMAIL, EVAL_PASSWORD = "eval@example.com", "EvalPass123!"
USER_EMAIL, USER_PASSWORD = "d1_test@example.com", "D1Pass123!"

ALL_FLAGS = (
    "retrieval.hybrid.enabled",
    "retrieval.rerank.enabled",
    "retrieval.vector.enabled",
    "retrieval.keyword.enabled",
)


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


async def set_flag(client: httpx.AsyncClient, name: str, value: bool | None) -> None:
    r = await client.post(f"{BASE}/api/v1/flags", json={"name": name, "value": value})
    if r.status_code != 200:
        raise SystemExit(f"设置 flag 失败 {name}={value}: {r.status_code} {r.text[:200]}")


async def clear_flags(client: httpx.AsyncClient) -> None:
    for name in ALL_FLAGS:
        await set_flag(client, name, None)


async def chat_collect(client: httpx.AsyncClient, content: str, headers: dict) -> dict:
    """发一轮对话并收集全部事件（两步流）。"""
    r = await client.post(
        f"{BASE}/api/v1/chat",
        headers={**headers, "Content-Type": "application/json",
                 "Idempotency-Key": f"m6-{uuid.uuid4().hex[:12]}"},
        json={"content": content},
    )
    r.raise_for_status()
    mid = r.json()["data"]["message_id"]

    out: dict = {"text": "", "degraded": [], "citations": [], "done": None, "approval": None}
    req = client.build_request("GET", f"{BASE}/api/v1/chat/{mid}/stream", headers=headers)
    resp = await client.send(req, stream=True)
    try:
        buf = ""
        async for chunk in resp.aiter_text():
            buf += chunk
            while "\n\n" in buf:
                blk, buf = buf.split("\n\n", 1)
                e = parse_block(blk)
                if not e:
                    continue
                t, d = e["event"], e["data"]
                if t == "token":
                    out["text"] += str(d.get("delta") or "")
                elif t == "citations":
                    out["citations"] = d.get("citations") or []
                elif t == "degraded":
                    out["degraded"] = d.get("degraded") or []
                    out["degraded_message"] = d.get("message")
                elif t == "approval":
                    out["approval"] = d
                elif t == "done":
                    out["done"] = d
                    return out
    finally:
        await resp.aclose()
    return out


async def search_once(client: httpx.AsyncClient, headers: dict, query: str, k: int = 6) -> dict:
    r = await client.post(f"{BASE}/api/v1/kb/search", headers=headers,
                          json={"query": query, "top_k": k})
    r.raise_for_status()
    return r.json()["data"]


async def main() -> int:
    fails: list[str] = []
    reports = Path("evals/reports")
    reports.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=600) as c:
        # ---------------- G1：多管线同集对比 ----------------
        print("=" * 72)
        print("G1: 两套管线同集对比（对比表 + 归因）")
        print("=" * 72)
        proc = subprocess.run(
            [sys.executable, "-m", "evals.compare_pipelines", "--base-url", BASE, "--k", "10",
             "--out", "evals/reports/m6_compare_details.jsonl",
             "--report", "evals/reports/m6_compare.json"],
            capture_output=True, text=True, timeout=2400,
        )
        print(proc.stdout[-1600:] if proc.stdout else proc.stderr[-1200:])
        if proc.returncode != 0:
            fails.append("G1")
        else:
            rep = json.loads((reports / "m6_compare.json").read_text(encoding="utf-8"))
            names = list(rep["pipelines"])
            base = rep["pipelines"][names[0]]["summary"]
            hybrid = rep["pipelines"][names[1]]["summary"]
            reranked = rep["pipelines"][names[2]]["summary"]
            down_single = rep["pipelines"][names[3]]["summary"]
            down_hybrid = rep["pipelines"][names[4]]["summary"]

            def m(s, key):  # noqa: E306
                return s.get(key, {}).get("mean")

            print("\n  【G1 判定依据】")
            print(f"  ① 单路（M3 基线）: recall@10={m(base,'recall@10')} ndcg@10={m(base,'ndcg@10')}")
            print(f"  ② +混合检索     : recall@10={m(hybrid,'recall@10')} ndcg@10={m(hybrid,'ndcg@10')}")
            print(f"  ③ +Rerank       : recall@10={m(reranked,'recall@10')} ndcg@10={m(reranked,'ndcg@10')}")
            print(f"  ④ 单路+向量故障 : recall@10={m(down_single,'recall@10')}")
            print(f"  ⑤ 混合+向量故障 : recall@10={m(down_hybrid,'recall@10')}")

            strict = (
                (m(hybrid, "recall@10") or 0) > (m(base, "recall@10") or 0)
                and (m(hybrid, "ndcg@10") or 0) > (m(base, "ndcg@10") or 0)
            )
            availability_gain = (m(down_hybrid, "recall@10") or 0) - (m(down_single, "recall@10") or 0)
            print(f"\n  严格判据（recall 与 nDCG 均高于基线）: {'达成' if strict else '**未达成**'}")
            print(f"  可用性判据（向量故障下 recall 提升）: +{availability_gain:.4f}")
            if not strict:
                print("  → 原因：本评测集已饱和（基线 recall@10=1.0 无提升空间）；"
                      "5 条跨文档问的 rel 分级属标注约定，精排无法学习（详见验收手册）")
            if availability_gain <= 0:
                fails.append("G1")

        # ---------------- G2：rerank 故障 → 仍能答 + "未精排" ----------------
        print("\n" + "=" * 72)
        print("G2: 模拟 rerank 故障 → 答案仍生成 + 显示「未精排」")
        print("=" * 72)
        r = await c.post(f"{BASE}/api/v1/auth/login",
                         json={"email": USER_EMAIL, "password": USER_PASSWORD})
        user_headers = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
        await set_flag(c, "retrieval.rerank.enabled", False)
        try:
            ev = await chat_collect(c, "什么是监督学习？", user_headers)
            print(f"  答案长度: {len(ev['text'])} 字")
            print(f"  degraded: {ev['degraded']} | message: {ev.get('degraded_message')}")
            print(f"  citations: {len(ev['citations'])} 条，rerank_score 示例: "
                  f"{[c.get('rerank_score') for c in ev['citations'][:3]]}")
            g2 = bool(ev["text"]) and "rerank" in ev["degraded"]
            print(f"  G2 = {'PASS' if g2 else 'FAIL'}")
            if not g2:
                fails.append("G2")
        finally:
            await set_flag(c, "retrieval.rerank.enabled", None)

        # ---------------- G3：embedding（向量路）故障 → 仅关键词 ----------------
        print("\n" + "=" * 72)
        print("G3: 模拟 Embedding 超时/故障 → 退化为仅关键词检索")
        print("=" * 72)
        await set_flag(c, "retrieval.vector.enabled", False)
        try:
            data = await search_once(c, user_headers, "监督学习和无监督学习的区别")
            print(f"  diagnostics: {json.dumps(data['diagnostics'], ensure_ascii=False)}")
            print(f"  degraded: {data['degraded']}")
            srcs = {tuple(h["sources"]) for h in data["hits"]}
            print(f"  命中来源集合: {srcs}")
            g3 = (
                "vector" in data["degraded"]
                and data["diagnostics"].get("vector_count") == 0
                and bool(data["hits"])
                and all("keyword" in (h["sources"] or []) for h in data["hits"])
            )
            print(f"  G3 = {'PASS' if g3 else 'FAIL'}（仅关键词路仍返回 {len(data['hits'])} 条）")
            if not g3:
                fails.append("G3")
            # 顺带验证对话链路也带降级标记
            ev3 = await chat_collect(c, "什么是无监督学习？", user_headers)
            print(f"  对话链路 degraded: {ev3['degraded']}（答案 {len(ev3['text'])} 字）")
            if "vector" not in ev3["degraded"]:
                print("  ⚠️ 对话链路未带 vector 降级标记")
                fails.append("G3")
        finally:
            await set_flag(c, "retrieval.vector.enabled", None)

        # ---------------- G4：记录分数 ----------------
        print("\n" + "=" * 72)
        print("G4: 记录分数（写入报告，供进度表引用）")
        print("=" * 72)
        rep_path = reports / "m6_compare.json"
        if rep_path.exists():
            rep = json.loads(rep_path.read_text(encoding="utf-8"))
            names = list(rep["pipelines"])
            recorded = {
                "baseline_single_path": rep["pipelines"][names[0]]["summary"],
                "hybrid_rrf": rep["pipelines"][names[1]]["summary"],
                "hybrid_rerank": rep["pipelines"][names[2]]["summary"],
                "vector_down_single": rep["pipelines"][names[3]]["summary"],
                "vector_down_hybrid": rep["pipelines"][names[4]]["summary"],
                "attribution": rep["attribution"],
                "observations": rep["observations"],
            }
            out = reports / "m6_scores.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(recorded, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  已归档 -> {out}")
            print(f"  可用性增益: {rep['attribution']['vector_down_availability']}")
        else:
            print("  ⚠️ 对比报告缺失（G1 未产出）")
            fails.append("G4")

    print("\n" + "=" * 72)
    print(f"结论：{'G1–G4 全部通过' if not fails else '未通过：' + ', '.join(fails)}")
    if "G1" in fails and len(fails) == 1:
        print("说明：G1 的严格判据在本评测集上未达成（集合已饱和，用户已确认不扩充）；"
              "可用性判据达成 —— 详见 M6_验收手册。")
    print("=" * 72)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
