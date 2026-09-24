"""多管线对比评测（M6，G1/G4 + 设计文档 §14.2 版本对比表）。

跑三条管线（同集、同 k、同语料），产出对比表与**归因拆解**：

    ① 单路向量（M3 基线）      flag: hybrid=off
    ② + 混合检索（关键词 + RRF）flag: hybrid=on, rerank=off
    ③ + Rerank                 flag: hybrid=on, rerank=on

指标：Recall@k / MRR@k / nDCG@k + 延迟 P50/P95（检索段，设计文档 §8.2 预算 P95 < 300ms）。

归因口径（§14.2 的「归因才是这张表的灵魂」）：
    · 混合检索贡献 = ② - ①
    · 精排贡献     = ③ - ②
另附两个**在饱和集上真正会动**的观测（不改变 golden 标注）：
    · top1 变化条数：某条 query 的排名第一文档是否变化（精排重排的直接影响）
    · 逐路命中证据：每条的 sources 分布（证明关键词路确实在工作）

用法：
    uv run python -m evals.compare_pipelines --base-url http://127.0.0.1:8080 \
        --out evals/reports/m6_compare_within.jsonl --report evals/reports/m6_compare.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.metrics import RetrievalMetrics, aggregate, evaluate_retrieval  # noqa: E402
from evals.run_eval import login, upload_corpus, wait_ready  # noqa: E402

PIPELINES: list[dict[str, Any]] = [
    {
        "name": "① 单路向量（M3 基线）",
        "flags": {"retrieval.hybrid.enabled": False, "retrieval.rerank.enabled": False},
    },
    {
        "name": "② + 混合检索（关键词 + RRF）",
        "flags": {"retrieval.hybrid.enabled": True, "retrieval.rerank.enabled": False},
    },
    {
        "name": "③ + Rerank",
        "flags": {"retrieval.hybrid.enabled": True, "retrieval.rerank.enabled": True},
    },
    # ④⑤ 依赖故障下的可用性（§8.3 降级矩阵的量化）—— 这是混合检索**最硬**的价值：
    # 向量路挂掉时，单路管线直接 0 召回（答不了），混合管线仍有关键词路可用。
    {
        "name": "④ 单路向量 + 向量路故障",
        "flags": {
            "retrieval.hybrid.enabled": False,
            "retrieval.rerank.enabled": False,
            "retrieval.vector.enabled": False,
        },
    },
    {
        "name": "⑤ 混合 + 向量路故障（关键词兜底）",
        "flags": {
            "retrieval.hybrid.enabled": True,
            "retrieval.rerank.enabled": True,
            "retrieval.vector.enabled": False,
        },
    },
]


async def set_flags(client: httpx.AsyncClient, base_url: str, flags: dict[str, bool]) -> None:
    for name, value in flags.items():
        r = await client.post(f"{base_url}/api/v1/flags", json={"name": name, "value": value})
        if r.status_code != 200:
            raise SystemExit(f"设置 flag 失败 {name}={value}: {r.status_code} {r.text[:200]}")


ALL_FLAGS = (
    "retrieval.hybrid.enabled",
    "retrieval.rerank.enabled",
    "retrieval.vector.enabled",
    "retrieval.keyword.enabled",
)


async def clear_flags(client: httpx.AsyncClient, base_url: str) -> None:
    for name in ALL_FLAGS:
        await client.post(f"{base_url}/api/v1/flags", json={"name": name, "value": None})


_FILE_BY_TITLE: dict[str, str] = {}


async def run_pipeline(
    client: httpx.AsyncClient,
    base_url: str,
    items: list[dict],
    k: int,
    pipeline: dict[str, Any],
) -> tuple[dict, list[dict]]:
    """跑一条管线：切 flag → 逐条检索 → 计算指标。返回 (聚合指标, 明细)。"""
    await set_flags(client, base_url, pipeline["flags"])
    metrics: list[RetrievalMetrics] = []
    latencies: list[float] = []
    details: list[dict] = []

    for item in items:
        import time

        t0 = time.perf_counter()
        resp = await client.post(
            f"{base_url}/api/v1/kb/search", json={"query": item["query"], "top_k": k}
        )
        resp.raise_for_status()
        payload = resp.json()["data"]
        latencies.append((time.perf_counter() - t0) * 1000)

        ranked: list[str] = []
        sources: list[str] = []
        for h in payload["hits"]:
            fname = _FILE_BY_TITLE.get(h["title"])
            if fname and fname not in ranked:
                ranked.append(fname)
            sources.extend(h.get("sources") or [])

        m = evaluate_retrieval(ranked, dict(item["expected_docs"]), k=k)
        metrics.append(m)
        details.append(
            {
                "id": item["id"],
                "ranked": ranked[:k],
                "top1": ranked[0] if ranked else None,
                "recall": round(m.recall_at_k, 4),
                "ndcg": round(m.ndcg_at_k, 4),
                "degraded": payload.get("degraded") or [],
                "sources_hit": sorted(set(sources)),
                "elapsed_ms": round(latencies[-1]),
                "diagnostics": payload.get("diagnostics") or {},
            }
        )

    summary = aggregate(metrics)
    summary["latency_p50_ms"] = {"ms": round(statistics.median(latencies))}
    summary["latency_p95_ms"] = {
        "ms": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))])
    }
    return summary, details


def _mean(summary: dict, key: str) -> float:
    return float(summary.get(key, {}).get("mean", 0.0))


async def run(
    *, base_url: str, email: str, password: str, golden: Path, docs_dir: Path | None, k: int
) -> dict:
    items = [
        json.loads(line)
        for line in golden.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results: dict[str, Any] = {"k": k, "n": len(items), "pipelines": {}}

    async with httpx.AsyncClient(timeout=120.0) as client:
        await login(client, base_url, email, password)
        if docs_dir is not None:
            title_by_file = await upload_corpus(client, base_url, docs_dir)
            _FILE_BY_TITLE.clear()
            _FILE_BY_TITLE.update(title_by_file)
            await wait_ready(client, base_url)
        elif not _FILE_BY_TITLE:
            resp = await client.get(f"{base_url}/api/v1/kb/documents")
            resp.raise_for_status()
            for d in resp.json()["data"]["items"]:
                _FILE_BY_TITLE[d["title"]] = d["title"]

        try:
            for pipeline in PIPELINES:
                print(f"--- 跑管线 {pipeline['name']} …", file=sys.stderr)
                summary, details = await run_pipeline(client, base_url, items, k, pipeline)
                results["pipelines"][pipeline["name"]] = {
                    "flags": pipeline["flags"],
                    "summary": summary,
                    "details": details,
                }
        finally:
            await clear_flags(client, base_url)  # 评测结束恢复配置默认

    # ---------------- 归因与对比（§14.2「归因才是这张表的灵魂」） ----------------
    names = [p["name"] for p in PIPELINES]
    name_base, name_hybrid, name_rerank, name_base_down, name_hybrid_down = names[:5]
    s1 = results["pipelines"][name_base]["summary"]
    s2 = results["pipelines"][name_hybrid]["summary"]
    s3 = results["pipelines"][name_rerank]["summary"]
    s_down_single = results["pipelines"][name_base_down]["summary"]
    s_down_hybrid = results["pipelines"][name_hybrid_down]["summary"]
    rk = f"recall@{k}"
    results["attribution"] = {
        "hybrid_contribution": {
            rk: round(_mean(s2, rk) - _mean(s1, rk), 4),
            f"ndcg@{k}": round(_mean(s2, f"ndcg@{k}") - _mean(s1, f"ndcg@{k}"), 4),
            f"mrr@{k}": round(_mean(s2, f"mrr@{k}") - _mean(s1, f"mrr@{k}"), 4),
        },
        "rerank_contribution": {
            rk: round(_mean(s3, rk) - _mean(s2, rk), 4),
            f"ndcg@{k}": round(_mean(s3, f"ndcg@{k}") - _mean(s2, f"ndcg@{k}"), 4),
            f"mrr@{k}": round(_mean(s3, f"mrr@{k}") - _mean(s2, f"mrr@{k}"), 4),
        },
        # 依赖故障下的可用性对比（混合检索的硬价值，非饱和指标）
        "vector_down_availability": {
            "single_path_recall": round(_mean(s_down_single, rk), 4),
            "hybrid_recall": round(_mean(s_down_hybrid, rk), 4),
            "delta": round(_mean(s_down_hybrid, rk) - _mean(s_down_single, rk), 4),
        },
    }

    # top1 变化（饱和集上真正会动的观测）
    d1 = {d["id"]: d for d in results["pipelines"][names[0]]["details"]}
    d2 = {d["id"]: d for d in results["pipelines"][names[1]]["details"]}
    d3 = {d["id"]: d for d in results["pipelines"][names[2]]["details"]}
    changed_by_hybrid = [i for i in d1 if d1[i]["top1"] != d2[i]["top1"]]
    changed_by_rerank = [i for i in d2 if d2[i]["top1"] != d3[i]["top1"]]
    keyword_used = [i for i in d2 if "keyword" in d2[i]["sources_hit"]]
    both_used = [i for i in d2 if set(d2[i]["sources_hit"]) >= {"vector", "keyword"}]
    downstream_degraded = [
        i for i in d3 if d3[i]["degraded"]
    ]
    results["observations"] = {
        "top1_changed_by_hybrid": {"count": len(changed_by_hybrid), "ids": changed_by_hybrid[:20]},
        "top1_changed_by_rerank": {"count": len(changed_by_rerank), "ids": changed_by_rerank[:20]},
        "keyword_path_used": {"count": len(keyword_used)},
        "both_paths_hit": {"count": len(both_used)},
        "degraded_in_main_pipeline": {"count": len(downstream_degraded)},
    }
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--email", default="eval@example.com")
    ap.add_argument("--password", default="EvalPass123!")
    ap.add_argument("--golden", default="evals/golden_v1.jsonl")
    ap.add_argument("--docs-dir", default=None, help="首次运行传 data/evaldocs")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--out", default=None, help="逐条明细 JSONL")
    ap.add_argument("--report", default="evals/reports/m6_compare.json")
    args = ap.parse_args()

    result = asyncio.run(
        run(
            base_url=args.base_url.rstrip("/"),
            email=args.email,
            password=args.password,
            golden=Path(args.golden),
            docs_dir=Path(args.docs_dir) if args.docs_dir else None,
            k=args.k,
        )
    )

    # 对比表（控制台可读版）
    k = result["k"]
    cols = [f"recall@{k}", f"mrr@{k}", f"ndcg@{k}", "latency_p50_ms", "latency_p95_ms"]
    print(f"\n{'管线':<28} " + " ".join(f"{c:>16}" for c in cols))
    for name in result["pipelines"]:
        s = result["pipelines"][name]["summary"]
        cells = []
        for c in cols:
            v = s.get(c, {}).get("mean", s.get(c, {}).get("ms"))
            cells.append(f"{v:>16}" if v is None else (f"{v:>16.4f}" if isinstance(v, float) else f"{v:>16}"))
        print(f"{name:<28} " + " ".join(cells))
    print("\n归因（差值）：")
    print(json.dumps(result["attribution"], ensure_ascii=False, indent=2))
    print("\n观测：")
    print(json.dumps(result["observations"], ensure_ascii=False, indent=2))

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved -> {args.report}")
    if args.out:
        with Path(args.out).open("w", encoding="utf-8") as f:
            for name in result["pipelines"]:
                for d in result["pipelines"][name]["details"]:
                    f.write(json.dumps({"pipeline": name, **d}, ensure_ascii=False) + "\n")
        print(f"details -> {args.out}")


if __name__ == "__main__":
    main()
