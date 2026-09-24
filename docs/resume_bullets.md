# 简历 bullet 定稿（M10-4）

> 原则（设计文档 §19）：**X/Y 等指标用实测数据填写，禁止编造；面试官会要求看原始数据。**
> 因此每条 bullet 都标注**数字来源**（仓库里的具体文件），并在末尾单列**因未实测而省略的宣称**。

## 定稿 bullet

### 1. Agent 运行时

> 设计并实现基于 LangGraph 的 **Plan-and-Execute Agent 运行时**：planner 产出 DAG 计划并由执行器按依赖波次**并行调度**（`asyncio.gather` + 并发上限）；ReAct 工具循环实现**四类显式终止条件**（无新调用 / 轮次上限 / token 预算 / 同参数重复调用）与预算降级（80% 停新工具、100% 出部分答案）；基于 Postgres Checkpointer 实现**进程崩溃后的中断恢复**。

- 数字来源：`M4_验收手册.md`（E1 并行执行时间戳重叠、E5 循环检测、E8 reducer 单测）；并行性能证据：`tests/integration/test_agent_graph.py`（两个 0.8s 工具步骤并行执行墙钟 < 1.6s，串行 ≥ 1.6s）
- 恢复能力来源：`M5_验收手册.md` F3（`docker kill` 后新进程批准并续跑完成）

### 2. 工具层与治理

> 基于 **MCP 协议**自建 4 个工具服务（sandbox / todo / search / email），并实现**客户端强制治理层**：风险等级取本地注册表（**不信任工具自述**，未注册工具 fail-closed 拒绝并落审计）、参数级风险升级（外发邮件 → L2 人工审批）、幂等去重、超时、结果截断与全文回取；将超时/重试/幂等/埋点/审计收敛到统一 `ToolRegistry`，新增工具**零成本继承**全部横切能力。

- 数字来源：`M4_验收手册.md` E7（未注册工具被拒 + `audit_logs.tool.denied`）、E6（埋点全路径）；
- 设计取舍与代价：`docs/adr/0004-tools-mcp.md`（含"RestrictedPython ≠ OS 隔离"的主动披露）

### 3. 生产级 RAG

> 生产级 RAG 检索链路：父块回溯 +（pgvector 向量 ‖ PostgreSQL 全文）**并行双路召回**经 **RRF 融合**（k=60）与云端 **Reranker** 精排；**检索层评测驱动**优化——自建 golden set 与评测脚本，重复测量确认检索层指标 **σ=0**（可做精确阈值门禁），在 68 条含真实语料的评测集上 **nDCG@10 0.9497 → 0.9567、MRR@10 0.9240 → 0.9485**，并完成**逐项归因**（混合检索 +0.0070 nDCG / +0.0147 MRR；精排 +0.0098 MRR，代价是检索段 P95 207ms → 463ms）。

- 数字来源：`evals/reports/m7_scores.json`、`docs/eval_report.md`
- 诚实边界：精排的 nDCG 变化为 −0.0013（68 条样本下接近噪声量级）——**面试时主动说明这一点的可信度高于只报好数字**

### 4. 多租户检索的工程风险处理

> 识别并处理 pgvector 在多租户过滤下的 **HNSW 召回坍塌**风险：落地 `hnsw.iterative_scan` 迭代扫描（连接级 GUC，旧版 pgvector 自动降级）+ 候选池放大（每路 200 条，用精排收敛），并在 ADR 中记录按租户规模的分层迁移策略（HASH 分区 / 部分索引）。

- 数字来源：无（**未实测坍塌比例**，见下方省略项清单）；策略与触发阈值：`docs/adr/0002-storage-pgvector.md`

### 5. 工程健壮性与可观测

> FastAPI async + SSE 流式（**执行与传输解耦**：任务投递 + Redis Stream 事件订阅 + `Last-Event-ID` 续读）· JWT 多租户（PostgreSQL **RLS 双保险**：`SET LOCAL` 事务内注入 + 仓储层显式过滤，跨租户返回 404 不泄露存在性）· **三层幂等**（API `Idempotency-Key` / 工具 `dedupe_key` / DB 唯一约束）· **依赖降级矩阵**（向量 / 关键词 / 精排三条链，降级必须显式可见）；Langfuse 全链路 trace + **双钩子 PII 脱敏**（25 条自动化断言）。

- 数字来源：`M1_验收手册.md`（B4 隔离 / B5 重放检测 / B7 幂等）、`M5_验收手册.md` F4（关页面不丢答案）、`M6_验收手册.md` G2/G3（降级角标）、`tests/unit/test_redact.py`
- 降级可用性：**向量依赖故障时 Recall@10 0.0000 → 0.9926**（`evals/reports/m7_scores.json`）

### 6. 交付与验证

> Docker Compose 六服务编排（postgres / redis / migrate / api / worker / caddy+web）**一条命令起栈**；GitHub Actions 三道门禁（后端 ruff+mypy+pytest / 前端 tsc+build / 迁移可加载性）全绿；每个里程碑配**可重复执行的验收脚本**（`scripts/m*-acceptance.py`）与验收手册，累计修复验收中发现的**真实缺陷**（如 checkpointer 静默失效、幂等实现自撞、SSE 缺失事件 id 导致续读失效）。

- 数字来源：`README.md` 徽章（CI 状态）、`scripts/ci_local.sh`（本地等价）、各 `M*_验收手册.md` 的"修复记录"章

### 取舍型 bullet（设计文档建议保留一条）

> 在 pgvector / Milvus / Elasticsearch / Chroma 之间评估后选择 **pgvector**：以"同库同事务 + 零额外组件"换掉分布式向量库的运维成本；并明确承认由此带来的代价——**中文全文检索只能应用层 jieba 分词 + `ts_rank_cd`，不声称 BM25**；多租户过滤下的 HNSW 召回风险靠迭代扫描与候选池放大缓解。

- 来源：`docs/adr/0002-storage-pgvector.md`

---

## 因未实测而**省略**的宣称（不写进简历）

| 草稿里的宣称 | 为什么省略 | 要补的话怎么做 |
|-------------|-----------|--------------|
| "深度研究类任务耗时从 Xs 降至 Ys" | 只有**集成测试**量级证据（2×0.8s 工具步骤并行墙钟 <1.6s），没有真实任务的串/并行对比 | 加一个 `max_parallel` flag，用同一任务跑 1 vs 3 并计时（需 LLM 额度） |
| "HNSW 过滤检索 Recall@10 由 X% 恢复至 Y%" | 当前语料仅 13 篇文档，**数据规模不足以复现召回坍塌** | 造数千 chunk 的语料并强过滤查询，测启用/关闭 iterative_scan 的召回差异 |
| "月度成本约 ¥X" | 无成本聚合作业；`messages.token_usage` 有原始数据但未汇总 | 加一个按天聚合 token 用量 × 单价的口径（并把"估算"字样写清） |
| "Faithfulness / Answer Relevancy 提升 X" | **未引入答案层指标**（RAGAS 已随范围裁剪） | 引入 RAGAS + judge 独立性设计 + 独立的噪声地板实验 |

> 这四项都写进了 `docs/eval_report.md` 的限制章与 `docs/adr/README.md` 的"未纳入范围"表——
> **面试被追问时，有"知道缺口在哪"的答案，比有一个编造的数字安全得多。**

## 使用建议

- 简历上**选 3 条**（推荐 3 / 5 / 6，分别覆盖 RAG 效果、工程健壮性、交付验证）。
- 第 4 条与"取舍型 bullet"用于**面试展开**（不是简历缩略），它们展示的是判断力而非技术栈。
- 被要求看原始数据时，直接打开 `evals/reports/m7_scores.json` 与 `docs/eval_report.md`——这两个文件就是为这一刻准备的。
