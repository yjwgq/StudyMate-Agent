# ADR-2：存储统一 PostgreSQL + pgvector

> 状态：**已实现**（M1 建表 + M3 检索 + M6 混合检索 + M7 评测）
> 实现落点：`infra/alembic/versions/0001_initial_schema.py`（DDL）、`apps/api/core/db.py`（连接层 GUC）、`agent/retrieval/search.py`（检索 SQL）
> 关键参数：cosine `<=>` · HNSW `m=16, ef_construction=128` · `hnsw.iterative_scan`

## 背景

业务数据（用户/文档/会话/审批/埋点）与向量数据（chunk 嵌入）都要存。独立向量库意味着两套存储、两套一致性、两套运维——对这个规模的个人助理平台不划算。

## 决策

业务数据与向量数据统一使用 **PostgreSQL 16 + pgvector**，不引入独立向量数据库。

理由：① 规模（约 1.5 万 chunk）远在 pgvector 舒适区内；② **同库同事务**——文档状态与 chunk 写入强一致，不存在"向量写成功但状态没更新"；③ 支持 JOIN 过滤（按文档/状态过滤后再检索）；④ 零额外组件。

## 替代方案

| 方案 | 放弃理由 |
|------|---------|
| Milvus | 需独立集群与运维，本项目用不上其分布式能力 |
| Elasticsearch / OpenSearch | 重（JVM），且要维护"向量库 + ES"双写一致性 |
| Chroma | v1 用过；单文件 SQLite 在 Windows 下有文件锁问题，且不支持行级多租户隔离 |

## 代价与主动披露的局限（**本 ADR 的核心价值**）

### 1. 中文全文检索：不声称使用 BM25

PostgreSQL 原生不支持中文分词，`zhparser` 需编译 SCWS（与纯 Windows 开发冲突）。
→ 方案：应用层 `jieba` 分词写入 `content_tokens`，生成 `tsv` 生成列（M2 落地，`worker/tasks/ingest.py`）。
→ **`ts_rank_cd` 与 BM25 不同**（无文档长度归一化、无 k1/b）。若确需 BM25，可引入 ParadeDB `pg_search` 或独立 Tantivy，代价是多一个组件——当前阶段不做。**面试时这条比"我用了 BM25"更值钱：知道自己用的不是 BM25。**

### 2. 多租户过滤下的 HNSW 召回坍塌（已识别并落地缓解）

HNSW 是近似最近邻，图搜索只扩展 `ef_search` 个候选。当 `WHERE user_id = ?` 选择性很高（如 1%）时，多数候选被过滤，返回的 k 条可能充斥无效填充，召回率显著下降。

**落地的分层策略**：

| 场景 | 策略 | 本项目状态 |
|------|------|-----------|
| 单租户 chunk < 5 万 | `SET hnsw.iterative_scan`（pgvector ≥ 0.8，图遍历中持续补页） | ✅ **已落地**（`apps/api/core/db.py` 的连接级 GUC，旧版 pgvector 自动降级） |
| chunk 5 万–50 万 | `chunks` 按 `user_id` HASH 分区 | ⏳ 未做（规模未到） |
| 少数重度租户 | 高选择性租户建部分索引 | ⏳ 未做（规模未到） |
| 召回阶段 | 候选池放大（各路 200 而非 50），靠精排收敛 | ✅ **已落地**（`HYBRID_TOP_K=200`，M6） |

**诚实说明**：当前评测语料只有 13 篇文档（chunk 数量少），**不足以复现召回坍塌**。我们做的是识别风险 + 落地迭代扫描与候选池放大两项缓解措施，**没有"召回率从 X% 恢复到 Y%"的实测数字**——不编造这个数字（见 `docs/resume_bullets.md` 的省略项清单）。

### 3. HNSW 索引构建成本

`ef_construction=128` 在建百万级索引时耗时与内存可观；本规模可接受。

## 影响

- 检索层能在一个 SQL 里同时用到"向量相似度 + 租户过滤 + 文档状态过滤"，这是 M6 混合检索与 M7 评测能快速迭代的基础。
- `vector(1024)` 与 `EMBED_DIM` 的耦合成为硬约束（ADR-8 要求运行时校验维度）。
