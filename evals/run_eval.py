"""v2 检索评测器（M3，§14.2.1 检索层指标）。

黑盒评测：登录 → 对每条 golden query 调 POST /kb/search → 把命中
映射到文档文件名（评测用户只上传 evaldocs 语料，title ↔ 文件名一一对应）
→ Recall@k / MRR@k / nDCG@k。

为什么走 HTTP 而不是直连 DB：
    评测的对象是「用户可感知的检索行为」，中间任何一层（RLS、RLS 之上的
    显式过滤、SQL 形状）都可能是回归点 —— 黑盒才能全部覆盖（D5 也基于它）。

用法（栈已启动、语料已上传）：
    uv run python -m evals.run_eval --base-url http://127.0.0.1:8000 \
        --email eval@example.com --golden evals/golden_v1.jsonl --k 10
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

from evals.metrics import RetrievalMetrics, aggregate, evaluate_retrieval

# title ↔ 文件名映射：gen_golden 的语料标题是「# {title}」，上传后
# documents.title = 文件名去后缀（kb.py 上传逻辑），评测用文件名对齐
_FILE_BY_TITLE: dict[str, str] = {}


async def login(client: httpx.AsyncClient, base_url: str, email: str, password: str) -> None:
    resp = await client.post(
        f"{base_url}/api/v1/auth/register",
        json={"email": email, "password": password, "display_name": "Eval Runner"},
    )
    if resp.status_code not in (200, 201, 409):  # 409 = 已注册，直接登录
        raise SystemExit(f"注册失败 {resp.status_code}: {resp.text[:200]}")
    resp = await client.post(
        f"{base_url}/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    resp.raise_for_status()
    token = resp.json()["data"]["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"


async def upload_corpus(
    client: httpx.AsyncClient, base_url: str, docs_dir: Path
) -> dict[str, str]:
    """上传评测语料（重复上传被 content_hash 去重，幂等）。返回 {文件名: title}。"""
    title_by_file: dict[str, str] = {}
    for path in sorted(docs_dir.glob("*.md")):
        with path.open("rb") as f:
            resp = await client.post(
                f"{base_url}/api/v1/kb/documents",
                files={"file": (path.name, f, "text/markdown")},
            )
        if resp.status_code not in (200, 201, 202):
            raise SystemExit(f"上传失败 {path.name}: {resp.status_code} {resp.text[:200]}")
        doc = resp.json()["data"]["document"]
        title_by_file[path.name] = doc["title"]
    return title_by_file


async def wait_ready(client: httpx.AsyncClient, base_url: str, timeout_s: float = 120.0) -> None:
    """轮询直到全部文档 ready（ingest 是 Celery 异步）。"""
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        resp = await client.get(f"{base_url}/api/v1/kb/documents")
        resp.raise_for_status()
        docs = resp.json()["data"]["items"]
        if docs and all(d["status"] == "ready" for d in docs):
            return
        if any(d["status"] == "failed" for d in docs):
            failed = [d for d in docs if d["status"] == "failed"]
            raise SystemExit(f"语料入库失败: {[(d['title'], d.get('error_message')) for d in failed]}")
        await asyncio.sleep(2)
    raise SystemExit(f"语料入库超时（{timeout_s}s）")


async def eval_one(
    client: httpx.AsyncClient, base_url: str, item: dict, k: int
) -> tuple[RetrievalMetrics, list[str]]:
    """单条 query：检索 → doc 维度映射（title→文件名）→ 指标。"""
    resp = await client.post(
        f"{base_url}/api/v1/kb/search", json={"query": item["query"], "top_k": k}
    )
    resp.raise_for_status()
    hits = resp.json()["data"]["hits"]

    file_by_title = _FILE_BY_TITLE
    ranked: list[str] = []
    for h in hits:
        fname = file_by_title.get(h["title"])
        if fname and fname not in ranked:  # 父块去重后再按 doc 去重
            ranked.append(fname)

    # expected_docs 的 key 是文件名；v2 hit 的 title 是上传时的净化标题
    relevant = {fname: rel for fname, rel in item["expected_docs"].items()}
    metrics = evaluate_retrieval(ranked, relevant, k=k)
    return metrics, ranked


async def run(
    *,
    base_url: str,
    email: str,
    password: str,
    golden_path: Path,
    docs_dir: Path | None,
    k: int,
    limit: int | None = None,
) -> dict:
    items = [
        json.loads(line)
        for line in golden_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if limit:
        items = items[:limit]

    async with httpx.AsyncClient(timeout=60.0) as client:
        await login(client, base_url, email, password)
        if docs_dir is not None:
            title_by_file = await upload_corpus(client, base_url, docs_dir)
            _FILE_BY_TITLE.clear()
            _FILE_BY_TITLE.update(title_by_file)
            await wait_ready(client, base_url)
        elif not _FILE_BY_TITLE:
            # 不带 --docs-dir 时从已有文档列表推导映射。
            # 上传时 documents.title = 原始文件名（**含后缀**），
            # golden 的 expected key 也是文件名 —— 原值即映射，勿再补后缀
            resp = await client.get(f"{base_url}/api/v1/kb/documents")
            resp.raise_for_status()
            for d in resp.json()["data"]["items"]:
                _FILE_BY_TITLE[d["title"]] = d["title"]

        all_metrics: list[RetrievalMetrics] = []
        misses: list[dict] = []
        latencies: list[float] = []
        for item in items:
            t0 = time.perf_counter()
            metrics, ranked = await eval_one(client, base_url, item, k)
            latencies.append(time.perf_counter() - t0)
            all_metrics.append(metrics)
            if metrics.recall_at_k < 1.0:
                misses.append(
                    {
                        "id": item["id"],
                        "query": item["query"],
                        "expected": list(item["expected_docs"]),
                        "ranked": ranked[:k],
                        "recall": metrics.recall_at_k,
                    }
                )

    result = aggregate(all_metrics)
    result["latency_p50_ms"] = {"ms": round(sorted(latencies)[len(latencies) // 2] * 1000)}
    result["latency_p95_ms"] = {
        "ms": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))] * 1000)
    }
    return {"summary": result, "misses": misses}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--email", default="eval@example.com")
    ap.add_argument("--password", default="EvalPass123!")
    ap.add_argument("--golden", default="evals/golden_v1.jsonl")
    ap.add_argument("--docs-dir", default=None, help="评测语料目录（缺省不重新上传）")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=None, help="结果 JSON 输出路径")
    args = ap.parse_args()

    result = asyncio.run(
        run(
            base_url=args.base_url.rstrip("/"),
            email=args.email,
            password=args.password,
            golden_path=Path(args.golden),
            docs_dir=Path(args.docs_dir) if args.docs_dir else None,
            k=args.k,
            limit=args.limit,
        )
    )

    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    if result["misses"]:
        print(f"\n--- 未全命中的 query（{len(result['misses'])} 条）---", file=sys.stderr)
        for m in result["misses"][:10]:
            print(
                f"  {m['id']} recall={m['recall']:.2f} expected={m['expected']} got={m['ranked']}",
                file=sys.stderr,
            )
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nsaved -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
