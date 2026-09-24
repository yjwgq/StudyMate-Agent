"""v1 Chroma 单路检索 baseline（M3-5，ADR-2）。

设计文档的硬要求（§1.2 / ADR-2）：
    「提升 Y 个点」如果没有可复现的对照物，在面试中是不成立的。
    v1 已废弃的 Chroma 单路向量检索正是最合适的 baseline —— 保留其最小
    实现，让 v2 的每一次检索改进都能对着同一把尺子量。

与 v1 的差异（刻意保留 v1 的关键特征，否则 baseline 失去意义）：
    - v1 分块：AdaptiveTextSplitter「lecture」策略 —— 定长 1024 字符 /
      重叠 128 / 按 \\n\\n 分隔，**没有父子分层**；
    - v1 检索：Chroma 默认 similarity_search_with_score，**单路向量**，
      无过滤（单用户时代）、无去重；
    - v1 embedding：本地 Ollama qwen3-embedding:4b。v2 全程云端
      （ADR-8），故此处换成同一云端 embedding 模型 —— 这是**唯一的
      必要偏离**：baseline 要对照检索策略，不是对照 embedding 提供商。
      报告对比结果时必须说明这一点。

用法：
    uv run python -m evals.baseline.v1_chroma_retriever --docs-dir data/evaldocs \
        --golden evals/golden_v1.jsonl --k 10
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from evals.metrics import RetrievalMetrics, aggregate, evaluate_retrieval

# v1 AdaptiveTextSplitter「lecture」策略参数（data_process/text_splitter.py）
V1_CHUNK_SIZE = 1024
V1_CHUNK_OVERLAP = 128


def v1_split(text: str) -> list[str]:
    """v1 的定长切分（简化复刻）：按段落聚合到 chunk_size，重叠 overlap。"""
    paras = [p for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= V1_CHUNK_SIZE:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            # 长段落硬切
            while len(p) > V1_CHUNK_SIZE:
                chunks.append(p[:V1_CHUNK_SIZE])
                p = p[V1_CHUNK_SIZE - V1_CHUNK_OVERLAP :]
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


def build_collection(persist_dir: Path, docs_dir: Path):
    """把语料灌进 Chroma（v1 单集合、无 user 维度）。"""
    import chromadb

    from evals.embedding_bridge import embed_texts

    client = chromadb.PersistentClient(path=str(persist_dir))
    col = client.get_or_create_collection(
        name="v1_baseline", metadata={"hnsw:space": "cosine"}
    )
    ids: list[str] = []
    texts: list[str] = []
    metadatas: list[dict] = []
    for path in sorted(docs_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for i, chunk in enumerate(v1_split(text)):
            ids.append(f"{path.stem}_{i}")
            texts.append(chunk)
            metadatas.append({"file": path.name, "title": path.stem})
    # 分批灌入（embedding 批量上限 10）
    for start in range(0, len(texts), 10):
        batch = texts[start : start + 10]
        col.add(
            ids=ids[start : start + 10],
            documents=batch,
            metadatas=metadatas[start : start + 10],  # type: ignore[arg-type]
            embeddings=embed_texts(batch),  # type: ignore[arg-type]
        )
    return col


def run(
    *, docs_dir: Path, golden_path: Path, k: int, limit: int | None = None
) -> dict:
    from evals.embedding_bridge import embed_texts

    persist_dir = Path(tempfile.mkdtemp(prefix="v1_chroma_"))
    try:
        col = build_collection(persist_dir, docs_dir)
        items = [
            json.loads(line)
            for line in golden_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if limit:
            items = items[:limit]

        metrics_all: list[RetrievalMetrics] = []
        misses: list[dict] = []
        for item in items:
            res = col.query(query_embeddings=[embed_texts([item["query"]])[0]], n_results=k)
            metas = res["metadatas"][0] if res["metadatas"] else []
            ranked: list[str] = []
            for m in metas:
                fname = m.get("file")
                if fname and fname not in ranked:
                    ranked.append(fname)
            m = evaluate_retrieval(ranked, dict(item["expected_docs"]), k=k)
            metrics_all.append(m)
            if m.recall_at_k < 1.0:
                misses.append(
                    {"id": item["id"], "query": item["query"], "ranked": ranked[:k]}
                )
        return {"summary": aggregate(metrics_all), "misses": misses, "n": len(items)}
    finally:
        shutil.rmtree(persist_dir, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs-dir", default="data/evaldocs")
    ap.add_argument("--golden", default="evals/golden_v1.jsonl")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    result = run(
        docs_dir=Path(args.docs_dir),
        golden_path=Path(args.golden),
        k=args.k,
        limit=args.limit,
    )
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    if result["misses"]:
        print(f"\n未全命中 {len(result['misses'])} 条", flush=True)
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"saved -> {args.out}")


if __name__ == "__main__":
    main()