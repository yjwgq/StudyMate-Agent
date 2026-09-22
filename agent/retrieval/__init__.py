"""检索链路 —— 由 M2 / M3 / M6 分阶段落地。

    parser.py     文档解析（M2 只用 PyMuPDF；MinerU/OCR 已裁剪出 MVP）
    splitter.py   父子分块（子 256 token / 父 1024 token / 重叠 10%）
    ingest.py     入库管线（jieba 分词 → tsv → embedding → 幂等写入）
    hybrid.py     混合检索：向量路 + 关键词路 → RRF 融合     [M6]
    rerank.py     精排（bge-reranker-v2-m3）与降级            [M6]
    rewrite.py    HyDE + 子问题拆分（并行投机 + 软超时）      [M6]
    citations.py  引用拼装与校验
"""
