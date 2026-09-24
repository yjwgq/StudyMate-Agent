# 架构决策记录（ADR）

本目录是项目**关键决策**的独立记录，每条含：背景 / 决策 / 替代方案 / 代价与局限 / 影响。
每条 ADR 都标注**实现状态**——设计文档写的是"打算怎么做"，这里写的是"**实际怎么做，以及为什么不同**"。

## 索引

| # | 决策 | 状态 | 与设计文档的差异（要点） |
|---|------|------|------------------------|
| [0001](./0001-agent-framework-langgraph.md) | Agent 框架选 LangGraph | 已实现 | 未用子图与 `Send`；ReAct 是节点内 async 函数 |
| [0002](./0002-storage-pgvector.md) | 存储统一 PostgreSQL + pgvector | 已实现 | `iterative_scan` 已落地；未做 HASH 分区（规模未到）；**不声称 BM25** |
| [0003](./0003-frontend-nextjs.md) | 前端 Next.js + 原生 fetch 流式 | 已实现 | 未引入 shadcn/ui；M5 起为**两步流**；Markdown sanitize 未做 |
| [0004](./0004-tools-mcp.md) | 工具体系 MCP + 本地治理层 | 已实现 | 多一个 `email` server；**按调用开短会话**（非常驻） |
| [0005](./0005-async-tasks-celery.md) | 异步任务 Celery + Redis | 已实现 | **消除异步阻抗**（全同步 psycopg3），而非 gevent 适配 |
| [0006](./0006-observability-langfuse.md) | 可观测性 Langfuse Cloud | 已实现 | v4 SDK 双 mask 钩子；trace 级 `userId` 未闭环 |
| [0007](./0007-evaluation-golden-set.md) | 自建检索层 golden set + 噪声地板 | 已实现 | **RAGAS 未引入**；golden 68 条（非 320）；σ=0 使门禁可精确 |
| [0008](./0008-models-cloud-only.md) | 模型统一走云端，配置分离 | 已实现 | reranker 实为 `qwen3.7-text-rerank`；未做模型指纹启动拒绝 |
| [0009](./0009-multi-tenancy-rls.md) | 多租户 RLS 共享库 | 已实现 | checkpoint 表的 schema 授权是额外一课（M5 才发现） |
| [0010](./0010-execution-transport-decoupling.md) | **执行与传输解耦**（投递 + Redis Stream） | 已实现 | 实现期新增：长连接装不下 HITL 的暂停 |
| [0011](./0011-retrieval-feature-flags.md) | **检索 feature flag**（配置 + Redis 覆写） | 已实现 | 实现期新增：评测与故障演练都靠它 |
| [0012](./0012-react-decision-memoization.md) | **ReAct 决策记忆化** | 已实现 | 实现期新增：保证重放一致与"批准即执行" |

> 0001–0009 来自设计文档 §3；**0010–0012 是实现期新增的决策**——它们不在原设计里，
> 但都是"不改就做不下去"的真实取舍，且每一条都有明确的替代方案与代价。

## 未纳入范围（设计文档有、MVP 不做）

| 项 | 原因 | 记录位置 |
|----|------|---------|
| RAGAS 答案层指标（Faithfulness 等） | 依赖 LLM judge（成本 + 随机性 + judge 独立性设计），范围裁剪 | ADR-0007 |
| 查询改写（HyDE / 子问题拆分） | 范围裁剪；`.env` 残留的 `HYDE_SOFT_TIMEOUT_MS` 与占位文件已清理 | `agent/retrieval/__init__.py` |
| 三级记忆系统（情景 / 语义 / 画像） | 范围裁剪（M8 移出 MVP） | `项目实施计划.md` §9 |
| Prometheus + Grafana 面板 | 范围裁剪（Langfuse 覆盖 LLM 观测；指标缺口已记录） | ADR-0006 |
| 成本聚合与告警 | 未实现（`messages.token_usage` 有原始数据，无汇总作业） | ADR-0008 |

## 命名约定

```
NNNN-kebab-case-title.md     例：0010-execution-transport-decoupling.md
```

新增 ADR 时：编号递增、**不要修改历史 ADR 的决策段**（如要推翻它，新写一条并在"替代方案"里引用旧编号）。
