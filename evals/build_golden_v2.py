"""构建 M7 评测集 golden_v2（M7-1，§14.2.2）。

相对 golden_v1 的两处变化：

1. **跨文档问的 rel 分级改为对称（2,2）** —— 修 M6 发现的标注口径问题。
   v1 用「查询里第一个术语所在文档记 rel=2、第二个记 rel=1」，但
   "A 和 B 分别是什么/有什么区别"这类问句对两个对象**同等权重**，
   该约定会惩罚任何按语义排序的精排（M6 实测：精排在 q046 修好、
   q047/q050 被判变差，而三者的差异只是标注约定）。

2. **新增 18 条真实语料题（人工出题与标注）** —— 语料是仓库内**为人撰写**的
   技术文档（README / M6 验收手册 / 设计文档 §8），比 v1 的锚点合成文更长、
   结构更复杂、术语更密集。题型分布刻意覆盖混合检索的差异化场景：
       专名精查（关键词路优势）4 条
       语义改写（向量路优势）5 条
       跨文档对比 4 条（对称分级）
       长尾/多术语 3 条
       时序/数字精查 2 条
   标注由人工阅读语料后写入（expected_docs 的真值来源是文档内容本身），
   脚本内置校验：期望文档必须存在于语料目录，且每条的锚点词确实出现在
   期望文档中（防"标注与语料不一致"的静默错误）。

用法：
    uv run python -m evals.build_golden_v2 \
        --v1 evals/golden_v1.jsonl --out evals/golden_v2.jsonl \
        --docs-dir data/evaldocs
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# ---------------- 18 条真实语料题（人工出题 + 人工标注） ----------------
# 字段说明：
#   query         贴近真实使用的问法（不是"XX 是什么"的模板句）
#   expected_docs {文件名: rel}；rel=2 必须命中，rel=1 相关
#   anchor        用于自检的锚点词（必须出现在期望文档的正文里）
#   kind          题型（用于报告里的分组统计）
NEW_ITEMS: list[dict] = [
    # ---- 专名精查（关键词路优势：术语是精确串，向量路容易"语义泛化"）----
    {
        "query": "RRF 融合的 k 参数取的是多少？为什么说它免调参？",
        "expected_docs": {"real_m6_acceptance.md": 2},
        "anchor": "k=60",
        "kind": "exact_term",
    },
    {
        "query": "给检索段设的延迟预算是多少？实测又是多少？",
        "expected_docs": {"real_design_rag.md": 2, "real_m6_acceptance.md": 1},
        "anchor": "300ms",
        "kind": "exact_term",
    },
    {
        "query": "ts_rank_cd 是不是 BM25？如果不是，差别在哪？",
        "expected_docs": {"real_design_rag.md": 2},
        "anchor": "ts_rank_cd",
        "kind": "exact_term",
    },
    {
        "query": "多租户过滤下近似最近邻召回会出什么问题，项目怎么缓解？",
        "expected_docs": {"real_design_rag.md": 2},
        # 语料里的实际写法是 hnsw.iterative_scan（自检逼出的精确措辞）
        "anchor": "hnsw.iterative_scan",
        "kind": "exact_term",
    },
    # ---- 语义改写（向量路优势：问法里没有原文术语）----
    {
        "query": "两路检索的结果最后是怎么合并成一份排名的？",
        "expected_docs": {"real_m6_acceptance.md": 2, "real_design_rag.md": 1},
        "anchor": "RRF 融合",
        "kind": "semantic",
    },
    {
        "query": "精排挂了以后系统会怎么办，用户能察觉吗？",
        "expected_docs": {"real_m6_acceptance.md": 2},
        "anchor": "未精排",
        "kind": "semantic",
    },
    {
        "query": "知识库文档入库时，为什么要先做去噪再分块？",
        "expected_docs": {"real_design_rag.md": 2},
        "anchor": "去噪",
        "kind": "semantic",
    },
    {
        "query": "回答里的事实性论断没有出处时，系统会做什么处理？",
        "expected_docs": {"real_design_rag.md": 2},
        "anchor": "groundedness",
        "kind": "semantic",
    },
    {
        "query": "这个项目的向量检索靠什么数据库能力支撑？",
        "expected_docs": {"real_design_rag.md": 2, "real_project_readme.md": 1},
        "anchor": "pgvector",
        "kind": "semantic",
    },
    # ---- 跨文档对比（对称分级：两篇地位相同）----
    {
        "query": "B+ 树索引和 RRF 融合各自解决什么问题？",
        "expected_docs": {"db_systems.md": 2, "real_m6_acceptance.md": 2},
        "anchor": "B+ 树",
        "kind": "cross_doc",
    },
    {
        "query": "父子分块和 B+ 树索引这两个设计分别是为哪种检索服务的？",
        "expected_docs": {"real_design_rag.md": 2, "db_systems.md": 2},
        "anchor": "父子分块",
        "kind": "cross_doc",
    },
    {
        "query": "epoll 和向量召回在这个系统里分别处在什么位置？",
        "expected_docs": {"operating_systems.md": 2, "real_design_rag.md": 2},
        "anchor": "epoll",
        "kind": "cross_doc",
    },
    {
        "query": "TF-IDF 和 RRF 都是排序相关的概念，它们一样吗？",
        "expected_docs": {"nlp_basics.md": 2, "real_m6_acceptance.md": 2},
        "anchor": "TF-IDF",
        "kind": "cross_doc",
    },
    # ---- 长尾 / 多术语 ----
    {
        "query": "如果既想让语义相近但用词不同的内容被召回，又想精确匹配专名，架构上怎么同时满足？",
        "expected_docs": {"real_m6_acceptance.md": 2, "real_design_rag.md": 1},
        "anchor": "关键词路",
        "kind": "long_tail",
    },
    {
        "query": "上线一个新检索策略时，怎么保证评测结果可复现、不靠感觉？",
        "expected_docs": {"real_m6_acceptance.md": 2, "real_design_rag.md": 1},
        "anchor": "feature flag",
        "kind": "long_tail",
    },
    {
        "query": "多租户场景下，为什么每一路召回的候选要放大到 200 条？",
        "expected_docs": {"real_design_rag.md": 2, "real_m6_acceptance.md": 1},
        "anchor": "每路 top 200",
        "kind": "long_tail",
    },
    # ---- 时序 / 数字精查 ----
    {
        "query": "项目从最小闭环到 Agent 运行时一共经历了哪几个里程碑，各自什么时候验收的？",
        "expected_docs": {"real_project_readme.md": 2},
        "anchor": "M4",
        "kind": "numeric",
    },
    {
        "query": "混合检索每一路召回多少条、融合后留多少条进精排？",
        "expected_docs": {"real_m6_acceptance.md": 2, "real_design_rag.md": 1},
        "anchor": "候选 50",
        "kind": "numeric",
    },
]

# v1 的 5 条跨文档问（M6 发现的分级问题）：把它们改对称
CROSS_DOC_IDS_V1 = {"q046", "q047", "q048", "q049", "q050"}


def fix_cross_doc_grading(item: dict) -> tuple[dict, bool]:
    """把 v1 跨文档问的 {a:2, b:1} 改成 {a:2, b:2}（对称）。返回 (item, changed)。"""
    if item["id"] not in CROSS_DOC_IDS_V1:
        return item, False
    graded = dict(item["expected_docs"])
    if len(graded) == 2 and sorted(graded.values()) == [1, 2]:
        changed = True
        item = {
            **item,
            "expected_docs": {k: 2 for k in graded},
            "grading_note": "跨文档问对称分级（M7 修正：原 2/1 是任意约定，会惩罚语义排序）",
        }
        return item, changed
    return item, False


# ---------------- 真实语料的可重建（data/ 不入库，必须能一键还原） ----------------
# 语料 = 仓库内真实文档的**快照**（README / M6 验收手册 / 设计文档 §8）。
# 与 M3 的合成语料一样：语料目录被 .gitignore 忽略，靠脚本重建。
REAL_CORPUS_SOURCES: list[tuple[str, str, str]] = [
    (
        "real_project_readme.md",
        "README.md",
        "# Personal Agent OS 项目说明（真实文档·节选自仓库 README）",
    ),
    (
        "real_m6_acceptance.md",
        "M6_验收手册.md",
        "# 生产级 RAG 与降级矩阵验收记录（真实文档·节选自 M6 验收手册）",
    ),
]


def write_corpus(docs_dir: Path, *, project_root: Path) -> list[Path]:
    """从仓库文档重建真实语料（幂等：每次覆盖为当前快照）。"""
    docs_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fname, src, header in REAL_CORPUS_SOURCES:
        text = (project_root / src).read_text(encoding="utf-8")
        out = docs_dir / fname
        out.write_text(header + "\n\n" + text, encoding="utf-8")
        written.append(out)

    # 设计文档只取「§8 RAG 管线设计」一节（其余章节与检索无关）
    design = (project_root / "docs/design/PersonalAgent_设计文档_v1.1.md").read_text(encoding="utf-8")
    lines = design.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("## 8. RAG 管线设计"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    out = docs_dir / "real_design_rag.md"
    out.write_text(
        "# RAG 管线设计（真实文档·节选自设计文档第 8 章）\n\n" + "".join(lines[start:end]),
        encoding="utf-8",
    )
    written.append(out)
    return written


def validate(items: list[dict], docs_dir: Path, *, check_anchor: bool = True) -> None:
    """校验期望文档存在、锚点词确实在其正文中（防标注与语料脱节）。"""
    corpus = {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(docs_dir.glob("*.md"))
    }
    problems: list[str] = []
    for item in items:
        for fname, rel in item["expected_docs"].items():
            if fname not in corpus:
                problems.append(f"{item['id']}: 期望文档不存在 {fname}")
                continue
            if rel not in (1, 2):
                problems.append(f"{item['id']}: 非法 rel={rel}")
        anchor = item.get("anchor")
        # 锚点只要出现在**任一**期望文档即可（跨文档问的锚点属于主文档）
        if check_anchor and anchor and not any(
            anchor in corpus.get(f, "") for f in item["expected_docs"]
        ):
            problems.append(f"{item['id']}: 锚点 {anchor!r} 不在任何期望文档中")
    if problems:
        raise SystemExit("golden_v2 校验失败：\n  " + "\n  ".join(problems))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1", default="evals/golden_v1.jsonl")
    ap.add_argument("--out", default="evals/golden_v2.jsonl")
    ap.add_argument("--docs-dir", default="data/evaldocs")
    ap.add_argument("--write-corpus", action="store_true",
                    help="从仓库文档重建真实语料（data/ 不入库，首次运行需带上）")
    args = ap.parse_args()

    if args.write_corpus:
        written = write_corpus(Path(args.docs_dir), project_root=Path.cwd())
        print(f"真实语料已重建: {[p.name for p in written]}")

    v1 = [
        json.loads(line)
        for line in Path(args.v1).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    fixed = 0
    items: list[dict] = []
    for it in v1:
        new_it, changed = fix_cross_doc_grading(it)
        fixed += int(changed)
        items.append(new_it)

    start = len(items) + 1
    for i, spec in enumerate(NEW_ITEMS, start=start):
        items.append(
            {
                "id": f"r{i:03d}",
                "query": spec["query"],
                "expected_docs": spec["expected_docs"],
                "domain": "真实语料",
                "kind": spec["kind"],
                "anchor": spec["anchor"],
                "source": "M7 人工出题与标注（语料为仓库内真实技术文档）",
            }
        )

    validate(items, Path(args.docs_dir))
    Path(args.out).write_text(
        "\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
        encoding="utf-8",
    )

    kinds: dict[str, int] = {}
    for it in items:
        k = it.get("kind", "v1_anchor")
        kinds[k] = kinds.get(k, 0) + 1
    print(f"golden_v2 写入 {len(items)} 条 -> {args.out}")
    print(f"  跨文档分级修正: {fixed} 条")
    print(f"  题型分布: {json.dumps(kinds, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
