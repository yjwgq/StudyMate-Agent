评测集与评测脚本目录。

规划内容（见设计文档 v1.1 §14）：
    golden_v1.jsonl           M3 首批 50 条（建立 baseline）
    golden_320.jsonl          M7 扩到 320 条
    baseline/                 v1 Chroma 单路检索的最小可复现实现（对照物）
    retrieval_eval.py         检索层指标：Recall@k / MRR / nDCG
    ragas_run.py              答案层指标：RAGAS 四项
    noise_floor.py            噪声地板实验（重复跑，测 σ）
    ci_eval.py                CI 冒烟子集（60 条）
    redteam/                  红队样本：注入 / 越狱 / SSRF / 记忆投毒
