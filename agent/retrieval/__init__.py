"""检索链路 —— 由 M2 / M3 / M6 分阶段落地。

    parser.py     文档解析（M2 只用 PyMuPDF；MinerU/OCR 已裁剪出 MVP）
    splitter.py   父子分块（子 256 token / 父 1024 token / 重叠 10%）
    ingest.py     入库管线（jieba 分词 → tsv → embedding → 幂等写入）
    hybrid.py     混合检索纯函数：查询侧分词/tsquery 构建 + RRF 融合（M6）
    search.py     检索编排：并行双路召回 → RRF → 精排 → 父块回溯 + 降级矩阵（M3/M6）
    context.py    上下文拼装与 RAG 提示词
    denoise.py    页眉页脚去噪

说明：查询改写（HyDE / 子问题拆分）已随范围收敛移出 MVP（见实施计划 §8），
故不在此列 —— 早期占位注释已删除。
"""
