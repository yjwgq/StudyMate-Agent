"""噪声地板实验（M3-7 / D6，§14.2.3）。

设计文档的问题陈述：
    RAGAS 指标由 LLM judge 产生，本身带随机性。未测波动范围就设
    「回退 2 个点即阻断合并」，会导致门禁频繁假红灯 → 被忽略 → 门禁失效。

M3 的现实：检索层指标（Recall/MRR/nDCG）是确定性计算，同一份代码、
同一批样本重复跑应当零方差。**但零方差本身是一个需要被证伪的假设**
（embedding 服务返回可能微抖、HNSW 近似检索可能有附加随机性），
所以 M3 的噪声地板实验回答的是：

    「检索层指标的重复测量波动是多少？」

结论（若测得 σ=0）不是「不用做噪声地板」，而是：
    1. M3 的检索指标可以进 CI 做**精确阈值门禁**（这是个好属性）；
    2. 答案层指标（RAGAS，M7）另有噪声，M7 必须重做本实验并给 2σ 阈值。

脚本行为：
    - 同一份 golden、同一套语料，重复 N 次（默认 5）跑 v2 检索评测；
    - 输出每次的各指标 + mean ± σ；
    - `--k` 同时报告 Recall@1/@5/@10 的稳定性（k 越小越敏感）。

用法：
    uv run python -m evals.noise_floor --runs 5 --out evals/reports/noise_floor_m3.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from evals.run_eval import run as run_eval


def mean_std(values: list[float]) -> tuple[float, float]:
    return (statistics.mean(values), statistics.pstdev(values) if len(values) > 1 else 0.0)


def summarize(runs: list[dict]) -> dict:
    """把 N 次评测的 summary 汇总成 mean ± σ。"""
    keys = [k for k in runs[0] if k.startswith(("recall@", "mrr@", "ndcg@"))]
    out: dict[str, dict[str, float]] = {}
    for key in keys:
        vals = [float(r[key]["mean"]) for r in runs]
        m, s = mean_std(vals)
        out[key] = {
            "mean": round(m, 4),
            "std": round(s, 4),
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
            # §14.2.3：门禁阈值 = 2σ（不是拍脑袋的 2 个点）
            "gate_2sigma": round(2 * s, 4),
            "runs": len(vals),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--email", default="eval@example.com")
    ap.add_argument("--password", default="EvalPass123!")
    ap.add_argument("--golden", default="evals/golden_v1.jsonl")
    ap.add_argument("--docs-dir", default=None, help="首次运行可传 data/evaldocs")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--out", default="evals/reports/noise_floor_m3.json")
    args = ap.parse_args()

    import asyncio

    runs: list[dict] = []
    for i in range(args.runs):
        print(f"--- run {i + 1}/{args.runs} ---", file=sys.stderr)
        result = asyncio.run(
            run_eval(
                base_url=args.base_url.rstrip("/"),
                email=args.email,
                password=args.password,
                golden_path=Path(args.golden),
                docs_dir=Path(args.docs_dir) if (args.docs_dir and i == 0) else None,
                k=args.k,
            )
        )
        runs.append(result["summary"])
        print(json.dumps(result["summary"], ensure_ascii=False), file=sys.stderr)

    summary = summarize(runs)
    report = {
        "milestone": "M3",
        "metric_layer": "retrieval",
        "runs": args.runs,
        "k": args.k,
        "per_metric": summary,
        "note": (
            "M3 检索层为确定性计算；若 σ=0，说明检索指标可做精确阈值门禁。"
            "答案层指标（RAGAS）的噪声地板必须在 M7 重做（§14.2.3）。"
        ),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nsaved -> {out}")


if __name__ == "__main__":
    main()