# Personal Agent OS — 个人 AI 助理平台设计文档 v1.1

> 版本：v1.2（施工蓝图 · 范围收敛版）
> 日期：2026-09-23
> 状态：开发中（M0–M3 已交付并验收，M4 进行中）
> 前身：StudyMate Agent v1（LangGraph 多 Agent 学习助手）
> 定位：面向个人用户的多用户 AI 助理平台，支持 MCP 工具生态、生产级 RAG、Agent 运行时与全链路可观测。
> v1.1 修订说明：本版根据《面试官视角评审报告》修订，补齐并发/幂等/降级/预算等工程盲区，修正 v1.0 中的技术事实错误，并为评测对比表补上**可复现的 baseline**（v1 只有检索实现，没有任何评测脚本）。

---

## 0. 阅读指引

### 0.1 本文档包含什么

- 架构决策（ADR）与技术选型理由（含**代价与替代方案**，面试可直接引用）
- 完整数据库 DDL、Agent 状态机、并发与幂等设计、降级矩阵、API 契约
- 评测方法论（含噪声地板、版本对比与归因）
- 里程碑范围与验收标准（施工依据见 `项目实施计划.md`）
- 非功能性需求、容量规划与成本模型（简历数据来源）

### 0.2 规则优先级与废止声明（重要）

本仓库中曾存在多份约束文件，与本文档存在直接冲突。**自 2026-09-22 起，以本文档为唯一权威来源。**

| 文件 | 原约束 | 处置 |
|------|--------|------|
| `.trae/rules/全局约束.md` | 禁止 Linux/WSL/**Docker**；向量库必须 **Chroma**；前端必须 **Streamlit**；LLM 必须 Ollama qwen3:4b | **废止**，移入 `docs/archive/v1-rules/` |
| `.trae/rules/项目硬性规则.md` | 同上（单模式版本） | **废止**，移入 `docs/archive/v1-rules/` |
| `CLOUD_MODEL_GUIDE.md` | "两种模式共享同一 Chroma 向量库，切换模式不影响已入库数据" | **仅对 LLM 切换成立**；对 Embedding 切换不成立（见 ADR-8）。该文件已随 v1 内容删除 |

废止理由：v1 的约束是为"单人 Windows 原生、零容器、轻量 demo"设定的目标函数；v2 的目标函数变为"可部署、多租户、可观测的工程代表作"，两者的技术选型前提不同。**保留旧约束会让实现陷入自相矛盾。**

> 面试要点：能清楚解释"目标函数变了所以选型要变，并且我为变更留了痕"，比假装从未有过旧约束更专业。

### 0.3 语言与约定

- 后端/Worker/Agent：Python 3.12；前端：TypeScript。
- **一切 I/O 优先 async/await**（含 Agent、检索、LLM 调用、DB）。
- 时间统一 UTC 存储，展示层按 `users.settings.timezone` 转换。
- 所有外部依赖调用**必须**显式声明超时与降级路径（见 §8.3 降级矩阵）。

---

## 1. 项目背景与目标

### 1.1 背景：从 v1 到 v2 的真实演进路径

**v1（StudyMate Agent）的现状**（已对仓库源码逐文件核实）：

| v1 已有能力 | 现状（已核实） | v2 处置 |
|------------|--------------|--------|
| LangGraph 路由→执行→反思固定工作流 | 可用 | 升级为 Plan-and-Execute + ReAct 动态编排（§7） |
| **Chroma 单集合单路向量检索** | 可用（`tools/vector_search.py`，含 Windows 文件锁重试） | **保留其最小实现作为评测 baseline**（见 ADR-2），v2 新建混合检索 |
| `retrieve_agent.py` 的 material / literature 双分支 | 可用 | 拆分：检索走 `retrieval/`，文献格式化保留为工具 |
| RestrictedPython + SymPy 计算沙箱 | 可用 | MCP 化 + 进程隔离 + 资源限制（§7.5、§13.5） |
| 本地（Ollama）/ 云端双模型工厂 | 可用 | v2 **只保留云端**（DeepSeek + 千问），本地模式不迁移（ADR-8） |
| FastAPI 后端 + 错误码→友好提示映射 | 可用（8 个错误码，多为 Chroma/Ollama 专属） | 作为起点扩展为完整错误码字典（附录 C） |
| 数据处理器（`doc_parser` / `text_splitter` / `dataset_clean` / `batch_build_kb`） | 可用 | 迁移并按职责重排（附录 A） |

**v1 的真实工程短板**（v2 要解决的）：

固定工作流而非真正的 Agent（无规划、无动态工具选择、无中断恢复）；单集合单路检索（无混合、无精排）；单用户无认证；无流式输出；无多租户隔离；无可观测性；**完全没有评测**（既无检索层指标，也无答案层指标）；无容器化与 CI。

> **关于评测 baseline 的关键决策**：v1 没有评测，也没有混合检索。因此 v2 的 §14.2 对比表**必须自带一个有明确实现的 baseline**——v1 的 Chroma 单路检索正是最合适的选择。**"提升 Y 个点"如果没有可复现的对照物，在面试中是不成立的。** 故 ADR-2 保留其最小实现（`evals/baseline/v1_chroma_retriever.py`），而不是把它连同数据一起废弃。

### 1.2 目标（北极星）

打造一个可作为一线大模型应用开发岗代表作的**个人 AI 助理平台**，具备真实产品的关键工程特征：

1. **Agentic**：DAG 任务规划 + 并行执行 + ReAct 工具循环 + 中断恢复 + 人工审批（HITL），MCP 工具生态。
2. **生产级 RAG**：混合检索 + RRF + Rerank，答案带引用，**可量化的检索层评测**（Recall@k / MRR / nDCG + 噪声地板）。
3. **企业工程化**：多租户、SSE 流式、幂等、依赖降级（显式标注）、Langfuse 追踪、Docker/CI。

### 1.3 非目标（MVP 明确不做）

- 移动端 App、语音输入/合成、图像生成
- GraphRAG / 知识图谱（列入 backlog，v2 候选）
- 插件市场、用户间协作分享
- 微服务拆分、Kubernetes、Kafka（个人项目过度设计；单体 + 异步 Worker 已足够）
- Java/Spring 混合架构（纯 Python，保证一人可控、叙事连贯）
- **RLHF / 模型微调**（本项目定位是应用工程，训练不是差异化点；LoRA 列为 v3 候选）

> 关于"多租户是否过度设计"的说明：本项目虽定位"个人助理"，但按**单实例多用户 SaaS** 设计。理由是：多租户隔离是"上线给真实用户用"的前置条件，也是简历上最有区分度的一类工程经验。若只做单用户，`user_id` 维度退化为常量即可，不影响架构。

### 1.4 角色与使用场景

| 场景 | 示例 | 关键工程挑战 |
|------|------|------------|
| 知识问答 | "我上传的那篇关于 RAG 评测的文章说了什么？" | 跨个人库检索、引用可溯源 |
| 事务代办 | "明天下午 3 点提醒我交周报" → 调用 todo 工具，需人工确认 | 幂等（重试不重复建）、时区 |
| 深度研究 | "调研 2026 年主流向量数据库对比，输出带引用报告" | **DAG 并行子任务**、长任务中断恢复、上下文预算 |
| 学习解题 | 数理题沙箱验算 | 沙箱资源限制 |

---

## 2. 总体架构

### 2.1 架构图

```
┌────────────────────────────────────────────────────────────────────────┐
│ 反向代理 Caddy/Nginx（HTTPS · SSE proxy_buffering off）                 │
└───────────────┬────────────────────────────────────┬───────────────────┘
┌───────────────▼────────────────────────────────────▼───────────────────┐
│ 前端  Next.js 15 (App Router) · TS · shadcn/ui                          │
│  聊天(SSE流式) · 知识库(含检索调试台) · 审批                                │
└───────────────┬────────────────────────────────────┬───────────────────┘
                │ HTTPS / SSE                         │ REST(JSON)
┌───────────────▼────────────────────────────────────▼───────────────────┐
│ FastAPI (async)  接入层                                                  │
│  JWT · 多租户中间件 · 幂等键 · 会话锁 · 请求校验                          │
│  Chat / KB / Retrieval / Approval                                       │
└───────┬───────────────────────────┬────────────────────────────────────┘
        │                           │
┌───────▼──────────────┐  ┌─────────▼─────────┐
│  Agent Runtime       │  │  Retrieval Service │
│  LangGraph           │  │  hybrid→RRF→rerank │
│  Planner(DAG)→ReAct  │  │  降级矩阵          │
│  →HITL  (并行执行)   │  │  citation/ground   │
│  Checkpointer(PG)    │  └───────────────────┘
│  会话锁 + 幂等        │
└───────┬──────────────┘
        │ MCP (stdio/HTTP)
┌───────▼──────────────────────────────────────────────────────────┐
│ MCP Servers: sandbox(自建) · todo(自建) · web-search(自建)          │
│ 工具治理层：本地策略注册表(fail-closed) · 描述净化 · 白名单 · 健康检查 │
└──────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│ Celery Worker：文档入库（独立 ingest 队列）                          │
└──────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│ 基础设施  PostgreSQL16+pgvector · Redis · Langfuse Cloud · Docker  │
│           Compose · GitHub Actions(lint/test)                       │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 关键设计原则（v1.1 新增）

1. **执行与传输解耦**：Agent 执行由 Runtime 独立推进并落库；SSE 只是订阅结果。用户关页面不影响执行，重连可补齐（§7.7）。
2. **降级显式化**：任何跳过精排或护栏的路径，UI 必须打出可见标记。偷偷降级是产品级事故（§8.3）。
3. **幂等优先**：所有写操作（API、工具、定时任务）必须有幂等键，因为系统里同时存在前端重试、SSE 重连、Celery 重试三个重试源（§7.6）。
4. **横切关注点收敛**：超时、重试、幂等、风险拦截、结果截断、埋点，全部收敛到 `ToolRegistry.invoke` 一层（§7.5）。
5. **Fail-closed**：未注册的工具、未声明的风险等级、未知的 MCP server，一律拒绝而非放行（§13.6）。

### 2.3 请求生命周期（一次带工具调用的问答）

```
前端 POST /api/v1/chat  [Header: Idempotency-Key]
 → 鉴权 / 租户上下文注入
 → 幂等键查重（命中则直接返回已有 message_id 的流）
 → 会话锁获取 (Redis SET NX, key=conversation_id)
       └─ 未获取到 → 409 CONFLICT「上一条还在处理中」
 → assistant 消息预落库 (status='streaming', seq=N)
 → Langfuse trace 开始
 → planner：产出 DAG 计划（Step + depends_on）
 → 并行执行：取依赖已满足的 steps，asyncio.gather（并发上限 3）
       · 每个 step → react_agent 子图（ReAct 循环，预算受控）
             ├─ 工具选择 → ToolRegistry.invoke（超时/重试/幂等/风险拦截）
             ├─ risk=L2 → interrupt(HITL) → approvals 表 → SSE 推审批事件
             ├─ 工具结果截断至 max_chars，超出部分存 full_ref
             └─ 上下文滚动压缩（超过 4 轮后）
       · 知识类 → retrieval（并行召回 + 软超时降级）
 → synthesize：汇总各 step，生成带引用答案
 → groundedness 校验；未通过则标注
 → 流式推送结束时：消息更新 status='completed'，写 citations，落 usage
 → 释放会话锁；trace 落 Langfuse（token/成本）
```

---

## 3. 架构决策记录（ADR）

### ADR-1：Agent 框架选 LangGraph

**决策**：使用 LangGraph 作为 Agent 编排框架。

**理由**：显式状态机（可读、可测）、Checkpointer（中断恢复的刚需）、子图（学习解题 skill 独立演进）、`interrupt`（HITL 原生支持）、`Send` API（DAG 并行扇出）。

**放弃的替代方案**：
| 方案 | 放弃理由 |
|------|---------|
| AutoGen | 对话式编排难持久化，状态不可枚举，HITL 需自行实现 |
| CrewAI | 角色模板化，流程可控性弱，难以实现精细的风险分级与中断 |
| 裸 LangChain / 自研循环 | 中断恢复与状态持久化要全部自研，投入产出比低 |

**代价**：图定义较啰嗦；LangGraph 处于 0.x→1.x 演进期，API 有 breaking change 风险。
**缓解**：节点基类 + 装饰器封装；锁定 langgraph 版本并在 CI 中做升级冒烟测试。

---

### ADR-2：存储统一 PostgreSQL + pgvector

**决策**：业务数据与向量数据统一使用 PostgreSQL 16 + pgvector，不引入独立的向量数据库。

**理由**：
1. 本项目规模（约 1.5 万 chunk，规划上限十万级，见 §5.1）远在 pgvector 舒适区内。
2. **业务数据与向量同库同事务**：文档状态与 chunk 写入可强一致，避免"向量写成功但业务状态没更新"的分布式一致性问题。
3. 支持 JOIN 过滤（按文档/标签/时间过滤后再检索），独立向量库需先取 ID 再回查，多一跳。
4. 零额外组件，`docker compose up` 少一个状态服务。

**放弃的替代方案**：
| 方案 | 放弃理由 |
|------|---------|
| Milvus | 需独立集群与运维；本项目规模用不上其分布式能力 |
| Elasticsearch / OpenSearch | 重（JVM、内存占用高），且要维护"向量库 + ES"双写一致性 |
| Chroma | v1 使用过；单文件 SQLite 在 Windows 下有文件锁问题（v1 已踩坑），且不支持多租户行级隔离 |

**代价与主动披露的局限**：

1. **中文全文检索**：PostgreSQL 原生不支持中文分词，`zhparser` 需编译 SCWS（与"纯 Windows 开发"冲突）。
   → **方案**：应用层用 `jieba` 分词后写入 `content_tokens` 列，再生成 `tsv`（见 §6）。分词器可替换、可加领域词典。
   → **不声称使用 BM25**：`ts_rank_cd` 与 BM25 不同（无文档长度归一化、无 k1/b）。若确需 BM25，可引入 ParadeDB `pg_search` 或独立 Tantivy 服务，代价是多一个组件，当前阶段不做。

2. **多租户过滤下的 HNSW 召回坍塌**（**本 ADR 的核心风险**）：
   HNSW 是近似最近邻，图搜索只扩展 `ef_search` 个候选。当 `WHERE user_id = ?` 的选择性很高（如 1%）时，绝大多数候选点被过滤，返回的 k 条中充斥无效填充，**召回率可坍塌至三成以下**。
   → **分层策略**（按租户规模选择，见下表）：

   | 场景 | 策略 | 说明 |
   |------|------|------|
   | 单租户 chunk < 5 万 | `SET hnsw.iterative_scan = relaxed_order`（pgvector ≥ 0.8）+ 提高 `hnsw.max_scan_tuples` | 成本最低，一行配置 |
   | 单租户 chunk 5 万–50 万 | `chunks` 表按 `user_id` **HASH 分区**，每分区独立 HNSW 索引 | 让索引规模与租户选择性解耦；对应用层透明 |
   | 少数重度租户 | 对高选择性租户建**部分索引** `WHERE user_id = '...'` | 适合"少量超重用户"场景 |
   | 召回阶段 | 候选池放大（各路召回 200 而非 50），靠 rerank 收敛 | 用精排换召回，代价是 rerank 成本 |

   **触发阈值与迁移路径**：在 §14.1 中监控"召回阶段过滤后有效率"指标，低于阈值时触发分区迁移。迁移用 Alembic 的在线分区脚本，详见 §6.5。

3. **HNSW 索引构建成本**：`ef_construction=128` 在建百万级索引时耗时与内存可观；本项目规模可接受，但需在容量规划（§5）中体现。

---

### ADR-3：前端 Next.js 15 + 原生 fetch 流式

**决策**：前端使用 Next.js 15（App Router）+ TypeScript + shadcn/ui，SSE 用原生 `fetch` + `ReadableStream` 消费。

**理由**：流式对话、工具调用过程渲染（折叠卡片）、审批交互在 React 生态有最成熟的库支持；shadcn/ui 提供企业级观感；SSR/App Router 便于后续分享页。

**为什么不用 Vercel AI SDK 的 `useChat` 默认协议**：AI SDK 使用自己的 data stream protocol，与本项目自定义的 SSE 事件协议（§11.3）不同；本项目需要自定义事件（`approval`、`citation`、`degraded`），默认 transport 无法承载，硬塞会污染语义。
→ **实际做法**：原生 `fetch` + `ReadableStream` 逐块解析自定义 SSE 事件（M0 起即如此），协议可扩展、前后端契约明确（用契约测试保证，见 §16）。

**放弃的替代方案**：Streamlit（交互能力弱，无法做审批与细粒度流式）、Vue（生态内 AI 组件较少）。

---

### ADR-4：工具体系采用 MCP + 本地治理层

**决策**：工具统一为 MCP 协议（stdio/HTTP），并**在客户端侧增加强制治理层**。

**理由**：MCP 是工具/资源/提示的标准协议，一次封装多端复用；同时是 2025–2026 的行业热点，具备简历差异化价值。

**必须同时落地的治理措施**（v1.0 缺失，v1.1 补齐，详见 §13.6）：

1. **风险等级来自本地策略注册表，不信任工具自述**。MCP 是开放协议，第三方 server 不会遵守我们的 `risk_level` 约定 → 未注册工具默认拒绝（fail-closed）。
2. **工具描述净化**：描述会被塞进 function calling schema，是注入载体（工具投毒）。入库前去指令性语句 + 长度上限。
3. **Server 白名单 + 固定版本**：禁用自动发现，变更需人工 review。
4. **权限最小化**：每个 server 只获得完成其职责所需的最小权限与目录。
5. **生命周期治理**：stdio server 崩溃/挂起会拖死 Agent → 健康检查 + 调用超时 + 自动重启 + 重启后校验工具列表。

**兜底**：不适合 MCP 的薄封装（内部 todo CRUD）允许直接 Python 函数工具，但必须实现统一 `ToolMeta` + `BaseTool` 接口，并同样经过治理层。

---

### ADR-5：异步任务用 Celery + Redis，但必须处理异步阻抗

**决策**：文档入库等长耗时任务使用 Celery + Redis。

**必须承认的技术阻抗（v1.0 未提）**：本项目约定"一切 I/O 优先 async"，而 **Celery 的 prefork worker 是同步模型**。在 Celery task 中调用 async 代码若用 `asyncio.run()`，会导致事件循环反复创建销毁、asyncpg/httpx 连接池无法复用，性能显著劣化。

**评估过的替代方案**：

| 方案 | 优势 | 劣势 | 结论 |
|------|------|------|------|
| **Celery** | 生态成熟、文档多、面试认知度高、beat 内置、重试/死信成熟 | 同步模型与 async 栈冲突，需适配层 | **选它**，但需适配（见下） |
| taskiq | async 原生、风格与 FastAPI 一致、有 Redis broker 与 beat 实现 | 生态较新，观测/重试能力不如 Celery 完善 | v2 候选 |
| arq | async 原生、轻量 | 功能少（无复杂路由/优先级）、社区小 | 备选 |
| APScheduler / 后台线程 | 零额外组件 | 不可持久化、不可重试、多进程下重复执行 | 仅用于进程内轻量定时 |

**适配层设计（必须实现）**：
1. Worker 使用 `--pool=gevent`，并在**进程内维护常驻事件循环**，task 入口用 `loop.run_until_complete()`（而非 `asyncio.run()`），以便复用连接池。
2. async 资源（asyncpg pool、httpx client、LLM client）在 worker 进程内**单例复用**，禁止每个 task 重建。
3. 明确 `acks_late=True`，并处理"任务执行成功但 ack 前进程崩溃"导致的重复执行 → 依赖任务本身的幂等性（§7.6）。

**任务队列隔离（v1.0 缺失）**：文档入库是重且慢的任务，必须独立队列与独立 worker，否则一个用户上传大文件会阻塞其他任务。
→ 队列：`ingest`（文档入库，独立 worker，可限并发）。

---

### ADR-6：可观测性选 Langfuse（默认云版）

**决策**：LLM 专项观测使用 **Langfuse Cloud 免费层**；OpenTelemetry 作为通用日志/指标标准输出。

**理由**：Langfuse 同时覆盖 trace/span、token 与成本计量、用户反馈打分、prompt 版本管理四件事，避免自建三套系统。SDK 与自托管一致，未来可无痛迁移。

**关于自托管的准确说明（更正 v1.0 表述）**：
v1.0 称 Langfuse 可"Docker 一键起"。**这不准确**：Langfuse v3 起，自托管栈需要 **ClickHouse（trace/observation）+ Redis（队列）+ S3/MinIO（事件大对象）+ 其自有 PostgreSQL**，即 4 个额外有状态服务。对一个个人项目，这部分运维成本与其收益不成比例。

**放弃的替代方案**：
| 方案 | 放弃理由 |
|------|---------|
| Langfuse 自托管 | 4 个额外有状态服务的运维成本，个人项目不划算；列为 v2 选项 |
| Phoenix (Arize) | 面板能力较弱，成本计量与 prompt 管理不足 |
| 纯 OpenTelemetry + 自建面板 | 要自己实现 LLM 语义约定、成本计算、反馈收集，工作量大且没差异化 |

**落地要求**：**trace 通道必须做 PII redaction**（§13.2 的脱敏必须显式覆盖 trace 的 input/output），凭据类字段永不出现在 trace 中。

---

### ADR-7：自建检索层 golden set，先建立噪声地板

**决策**：评测采用自建检索层指标（Recall@k / MRR / nDCG），以固定 golden set + 版本对比表驱动优化。

**理由**：检索层指标能回答"效果变好是因为召回变好还是排序变好"——这是归因分析的前提。检索层指标由 v2 自建（v1 无任何评测脚本），实现量约 100 行。

→ **先做噪声地板实验**：同一份代码、同一批样本、固定 seed 与 temperature，重复评测 5 次，得到每项指标的 `mean ± σ`，作为解读版本差异的误差基准（差异 < 2σ 视为噪声）。
→ 对比表须带**逐项归因**：多少点来自混合召回、多少点来自精排。

详见 §14.2。

---

### ADR-8：模型统一走云端，LLM 与 Embedding 配置必须分离

**决策**：全程使用**云端模型**，不部署本地模型（不引入 Ollama / vLLM / 本地 Embedding 服务）。但 LLM 与 Embedding 的配置**必须分离**——因为两者影响面完全不同。

**为什么不部署本地模型**：

1. 本项目定位是**应用工程**代表作，模型推理本身不是差异化点；本地部署会引入 GPU 依赖、镜像体积、显存调优等与主题无关的复杂度。
2. **本地小模型（7B 级）的输出质量会掩盖检索层的问题**——评测时无法区分"检索没召回"和"模型没用好召回的内容"，归因会失真。这直接破坏 §14 的评测价值。
3. 配置组合与测试矩阵减半。
4. 云端成本可控：按 200 次/天对话量级约 ¥420/月（见 §5.2）。

**为什么要分离 LLM 与 Embedding 配置**：

| 配置 | 影响范围 | 变更后果 |
|------|---------|---------|
| LLM | 仅影响生成 | **无副作用**，随时可改 |
| Embedding | 影响**整个向量库** | 不同模型的向量空间**不兼容**，变更后**必须全量重嵌入** |

v1 的 `CLOUD_MODEL_GUIDE.md` 称"切换模式不影响已入库数据"——**这句话只对 LLM 成立，对 Embedding 不成立**。若两者共用一个配置项、或在一次提交里同时改掉，会静默损坏整个知识库。

**实现要求**：
1. LLM 与 Embedding 使用**独立的配置块**（`LLM_*` 与 `EMBED_*`），互不干扰。
2. `EMBED_MODEL` 或 `EMBED_DIM` 变更时，**启动自检直接拒绝启动**，并提示运行重嵌入脚本。
3. `EMBED_DIM` 在库中锁定（`chunks.embedding vector(1024)`），启动时校验实际返回维度与 DDL 一致。
4. **Reranker 也必须独立**：reranker 分数的绝对值依赖具体模型，§8.4 中的引用相关性阈值必须**按 reranker 模型分别标定**，否则换模型后阈值失效。

**选定的服务商（全部走 OpenAI 兼容接口，统一封装在 `agent/provider/`）**：

| 角色 | 服务商 | 约束 |
|------|--------|------|
| LLM | **DeepSeek** | 不接 OpenAI / Azure OpenAI |
| Embedding | **通义千问 text-embedding**（DashScope 兼容模式） | 返回维度必须是 **1024**，与 DDL 一致 |
| Reranker | `qwen3.7-text-rerank` | MVP 走云端 API（DashScope 原生 text-rerank 端点）|

**放弃的替代方案**：

| 方案 | 放弃理由 |
|------|---------|
| 本地 Ollama（v1 的做法） | 需 GPU 或大内存；小模型输出质量会掩盖检索层问题、破坏归因；与"应用工程"定位无关 |
| 云端 + 本地双模式并存 | 配置组合与测试矩阵翻倍，而本地模式在本项目中没有必须存在的场景 |
| 多云端服务商自动路由 | 增加鉴权/限流/降级的复杂度，MVP 无收益 |

**代价（必须承认）**：
1. **完全依赖网络与第三方可用性**——服务商抖动即影响服务，需靠 §8.3 的降级矩阵缓解。
2. **成本随用量线性增长**——靠日配额与成本监控控制，见 §5.2。
3. **用户数据需发送至第三方**——这是云端方案的固有代价；敏感数据靠 §13.2 的 PII 脱敏降低暴露面，但不能消除。

---

### ADR-9：多租户隔离采用"共享库 + 行级 tenant_id + RLS"

**决策**：共享库 + 每表强制 `user_id` + Repository 层校验 + PostgreSQL RLS 双保险。

**理由**：租户数有限（个人助理场景），schema-per-user 会导致迁移脚本数量爆炸。

**必须注意的实现细节（v1.0 缺失，这些是"是否真做过"的必考点）**：
1. **`SET LOCAL` 的作用域**：RLS 策略读 `current_setting('app.user_id')`，必须在**事务内**用 `SET LOCAL` 设置——因为 asyncpg 是连接池复用，会话级 `SET` 会**泄漏到下一个请求**。
2. **必须 `FORCE ROW LEVEL SECURITY`**：表 owner 默认**绕过** RLS，不加 FORCE 则策略形同虚设。
3. **后台任务需要专用角色**：Celery worker 需要跨租户访问（如扫描所有用户到期提醒）→ 使用**独立的、BYPASSRLS 的 DB 角色**，且该角色的连接串不与 API 共享。
4. **迁移脚本**用另一个 owner 角色，避免 RLS 阻碍 DDL。
5. **测试必须包含跨租户越权用例**（A 用户读 B 用户数据 → 403/空结果），纳入 CI 必过项。

---

## 4. 技术栈定稿

| 层 | 选型 | 版本约束 | 备注 |
|----|------|---------|------|
| 语言 | Python | 3.12 | |
| Web | FastAPI、Uvicorn、pydantic v2 | fastapi>=0.115 | |
| Agent | langgraph、langchain、mcp (Python SDK) | langgraph>=0.3 | checkpoint-postgres |
| 模型 | openai SDK（云服务商 OpenAI 兼容接口） | — | 全程云端：DeepSeek(LLM) + 千问(Embedding)，见 ADR-8 |
| 异步任务 | celery[redis]、redis-py | celery>=5.4 | gevent pool + 常驻事件循环 |
| 数据库 | PostgreSQL 16、pgvector、SQLAlchemy 2 (async)、Alembic | — | pgvector>=0.8（iterative_scan） |
| 中文分词 | jieba | — | 应用层分词写入 `content_tokens` |
| 检索 | pgvector HNSW、tsvector/ts_rank_cd、qwen3.7-text-rerank | — | 阈值按模型标定 |
| 解析 | PyMuPDF（文本型）、unstructured/MinerU（表格）、PaddleOCR（扫描件） | — | 按类型路由 |
| 鉴权 | python-jose、passlib[bcrypt]、OAuth2 Password Flow | — | refresh token 轮转 |
| 加密 | cryptography (AES-256-GCM) | — | 第三方凭据 envelope encryption |
| 观测 | langfuse（Cloud）、structlog、OpenTelemetry | — | trace 侧 PII redaction |
| 评测 | ragas、datasets、pytest | — | 需测噪声地板 |
| 前端 | Next.js 15、React 19、TS 5、Tailwind、shadcn/ui、Vercel AI SDK | — | 自定义 transport |
| 反代 | Caddy（自动 HTTPS，配置简单） | — | **SSE 必须 `proxy_buffering off`** |
| 部署 | Docker、Docker Compose、GitHub Actions | — | |
| 质量 | ruff、mypy、pytest、pre-commit | — | |

---

## 5. 非功能性需求与容量规划（v1.1 新增）

> 面试官必问："你算过钱吗？存得下吗？扛得住吗？" 本节就是答案。

### 5.1 目标规模假设

| 项 | 假设 | 说明 |
|----|------|------|
| 目标用户 | ≤ 10（自用 + 演示） | 后续按 100 人规划余量 |
| 单用户文档 | 50 份 × 平均 20 页 | 含课堂讲义、论文等 |
| 单文档 chunk 数 | 20 页 × 1.5 chunk/页 ≈ 30 | 按 256 token 子块估算 |
| **chunk 总量** | 10 × 50 × 30 = **15,000** | 远低于 pgvector 舒适区（百万级） |
| 向量存储 | 15,000 × 1024 × 4B ≈ **60 MB** + HNSW 索引开销 | 索引约为原始向量 1–1.5 倍 → **< 150 MB** |
| 文档原文存储 | 10 × 50 × 1 MB ≈ 500 MB | 对象存储或本地卷 |
| 日均对话 | 10 人 × 20 次 = 200 次/天 | |

### 5.2 成本模型

| 项 | 假设 | 估算 |
|----|------|------|
| 单次对话输入 token | system + 上下文 + 历史 ≈ 4,500 | |
| 单次对话输出 token | ≈ 800 | |
| 单次对话 LLM 成本 | 按云端主力模型单价估算 | **¥0.02 – ¥0.05** |
| 单次对话含检索成本 | + rerank + groundedness 的附加 token | **×1.2 倍** |
| 嵌入成本 | 15,000 chunk 一次性 ≈ 300 万 token | 一次性，可忽略 |
| **月度 LLM 成本** | 6,000 次/月（200/天 × 30）× ¥0.05 **× 1.2** | **≈ ¥360/月** |
| 服务器成本 | 单机 VPS（4C8G） | ≈ ¥60/月 |
| Langfuse | Cloud 免费层 | ¥0 |
| **合计** | | **≈ ¥420/月** |

### 5.3 SLO（服务等级目标）

| 指标 | 目标 | 测量方式 |
|------|------|---------|
| 首 token 延迟 P95 | < 2.5s | Langfuse span |
| 检索段延迟 P95 | < 300ms | Langfuse span |
| 端到端对话成功率 | > 98%（不含用户主动取消） | 消息 status 统计 |
| 可用性 | 99%（个人项目不承诺更高） | 外部探针 |
| 降级触发率 | < 5% | 降级计数器 |

> **两组数字的关系**：本表的数值是**目标值（应达到）**；§8.3 降级矩阵中的超时是**容忍上限（超过即触发降级）**。上限高于目标——例如检索段目标 P95 < 300ms，而向量路超时阈值为 500ms，意味着"常态应在 300ms 内，超过 500ms 才降级"。二者不可混用。

### 5.4 明确不承诺的事

- 不做多地域部署、不做读写分离、不做跨机房容灾。
- 不承诺 99.9% 可用性（个人项目成本不允许，虚假承诺反而减分）。
- 不承诺支撑万级并发（单体架构的上限，要有自知之明）。

---

## 6. 数据模型设计（PostgreSQL DDL）

### 6.1 通用约定

- 所有表含 `created_at / updated_at TIMESTAMPTZ`；业务表强制 `user_id` 并启用 RLS。
- 所有外键默认 `ON DELETE CASCADE`（用户注销即全量清理），例外处显式注释。
- 所有写操作需幂等键（见 §7.6）。

### 6.2 扩展与角色

```sql
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector >= 0.8（需 iterative_scan）
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid

-- 角色分离（RLS 相关，见 ADR-9）
-- app_api     : API 角色，受 RLS 约束
-- app_worker  : Worker 角色，BYPASSRLS（跨租户扫描定时任务）
-- app_owner   : 迁移角色，拥有表所有权
```

### 6.3 用户与认证

```sql
CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  display_name  TEXT,
  role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),  -- 新增：支撑 /admin
  settings      JSONB NOT NULL DEFAULT '{}',   -- timezone / briefing_time / 关注主题 / 模型偏好
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  last_login_at TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE refresh_tokens (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL,
  family_id  UUID NOT NULL,              -- 令牌家族（轮转链），用于重放检测
  expires_at TIMESTAMPTZ NOT NULL,
  revoked    BOOLEAN NOT NULL DEFAULT FALSE,
  revoked_reason TEXT,                   -- logout / rotated / reuse_detected
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX refresh_tokens_hash_idx ON refresh_tokens(token_hash);
CREATE INDEX refresh_tokens_user_idx ON refresh_tokens(user_id, expires_at DESC);
```

**刷新令牌轮转与重放检测**（v1.0 缺失）：每次刷新时旧 token 立即 `revoked` 并签发新 token（同 `family_id`）。**若检测到已 revoked 的 token 被再次使用，判定为凭据泄露 → 撤销该 family 下全部 token 并告警。**

### 6.4 会话与消息

```sql
CREATE TABLE conversations (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title         TEXT,
  last_message_at TIMESTAMPTZ,           -- 最近一条消息时间（会话列表排序用）
  last_consolidated_at TIMESTAMPTZ,      -- 巩固水印（增量巩固）
  archived      BOOLEAN NOT NULL DEFAULT FALSE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX conversations_user_idx ON conversations(user_id, last_message_at DESC);

CREATE TABLE messages (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  seq             INT NOT NULL,                          -- 会话内递增序号
  role            TEXT NOT NULL CHECK (role IN ('user','assistant','tool','system')),
  content         TEXT,
  status          TEXT NOT NULL DEFAULT 'completed'
                  CHECK (status IN ('streaming','completed','interrupted','cancelled','failed')),
  tool_calls      JSONB,                                 -- 工具调用轨迹（摘要）
  citations       JSONB,                                 -- 引用列表
  degraded        JSONB,                                 -- 本轮触发的降级标记（见 §8.3）
  token_usage     JSONB,
  trace_id        TEXT,
  feedback        SMALLINT,                              -- -1 点踩 / 0 无 / 1 点赞
  feedback_note   TEXT,
  superseded_by   UUID,                                  -- "重新生成"时指向新消息
  error           JSONB,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (conversation_id, seq)
);
CREATE INDEX messages_conv_seq_idx ON messages(conversation_id, seq);
CREATE INDEX messages_streaming_idx ON messages(status) WHERE status = 'streaming';
```

**为什么需要 `seq` 与 `status`**：支撑流式落库（流开始即写入 `streaming`）、断线补齐（按 seq 增量拉取）、以及"重新生成不覆盖历史"。

### 6.5 知识库

```sql
CREATE TABLE documents (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title         TEXT NOT NULL,
  source        TEXT,                    -- 原始 URL 或文件名
  source_type   TEXT NOT NULL CHECK (source_type IN ('upload','webclip','note')),
  status        TEXT NOT NULL DEFAULT 'processing'
                CHECK (status IN ('processing','ready','failed')),
  content_hash  TEXT NOT NULL,           -- 去重：同用户同一文件不重复入库
  parser_used   TEXT,                    -- pymupdf / mineru / ocr（解析质量归因用）
  chunk_count   INT,
  error_message TEXT,
  retry_count   INT NOT NULL DEFAULT 0,
  meta          JSONB NOT NULL DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, content_hash)
);
CREATE INDEX documents_user_status_idx ON documents(user_id, status);

CREATE TABLE chunks (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id    UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  parent_id      UUID REFERENCES chunks(id) ON DELETE CASCADE,
  chunk_type     TEXT NOT NULL CHECK (chunk_type IN ('parent','child')),
  ord            INT NOT NULL,           -- 文档内顺序，父块回溯用
  content        TEXT NOT NULL,
  content_tokens TEXT,                   -- jieba 分词结果（空格连接），供 tsv 生成
  meta           JSONB NOT NULL DEFAULT '{}',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- tsv 用生成列，避免"列永远为 NULL"的静默失效（v1.0 的 bug）
ALTER TABLE chunks
  ADD COLUMN tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content_tokens, ''))) STORED;

-- 仅子块建向量（父块不参与召回，避免召回父子重复）
ALTER TABLE chunks ADD COLUMN embedding vector(1024);
ALTER TABLE chunks ADD CONSTRAINT chunks_child_has_embedding
  CHECK ((chunk_type = 'child') = (embedding IS NOT NULL));

CREATE INDEX chunks_vec_idx ON chunks
  USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128);
CREATE INDEX chunks_tsv_idx ON chunks USING gin(tsv);
CREATE INDEX chunks_user_idx ON chunks(user_id);
CREATE UNIQUE INDEX chunks_doc_ord_uidx ON chunks(document_id, chunk_type, ord);  -- 入库幂等兜底（§7.6）
CREATE INDEX chunks_parent_idx ON chunks(parent_id) WHERE parent_id IS NOT NULL;
```

**分块策略**：父子分块（small-to-child / large-to-parent）。子块 256 token 用于精准召回，命中后回溯父块 1024 token 喂模型；重叠 10%。
**父块是否入库**：父块入库但不带向量（`embedding IS NULL`，由 CHECK 约束保证），仅作为回溯目标。这样检索只会命中子块，天然避免父子重复召回。

**维度一致性**：`EMBED_DIM` 在库中锁定；启动时校验实际返回维度与 DDL 一致，不一致直接拒绝启动。更换 Embedding 模型必须跑重嵌入脚本（见 ADR-8）。

**分区迁移路径（ADR-2 第 2 条）**：当单租户 chunk 超过 5 万时，用 Alembic 在线迁移为 `PARTITION BY HASH (user_id)`（16 个分区），每分区独立 HNSW 索引。迁移期间用双写 + 回填 + 切换读路径三步走。

### 6.6 三级记忆（拆表以同时满足"唯一"与"审计"）

```sql
CREATE TABLE episodic_memories (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
  summary         TEXT NOT NULL,
  embedding       vector(1024),
  importance      REAL NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
  happened_at     TIMESTAMPTZ NOT NULL,
  archived        BOOLEAN NOT NULL DEFAULT FALSE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX episodic_vec_idx ON episodic_memories
  USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128);
CREATE INDEX episodic_user_time_idx ON episodic_memories(user_id, happened_at DESC);

CREATE TABLE semantic_memories (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL CHECK (kind IN ('profile','preference','fact','instruction')),
  key           TEXT NOT NULL,
  value         TEXT NOT NULL,
  embedding     vector(1024),
  confidence    REAL NOT NULL DEFAULT 0.8 CHECK (confidence BETWEEN 0 AND 1),
  hit_count     INT NOT NULL DEFAULT 0,
  version       INT NOT NULL DEFAULT 1,
  source_episode_id UUID REFERENCES episodic_memories(id) ON DELETE SET NULL,
  needs_confirmation BOOLEAN NOT NULL DEFAULT FALSE,   -- 记忆安全：待用户确认
  last_used_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, kind, key)
);
CREATE INDEX semantic_vec_idx ON semantic_memories
  USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128);
CREATE INDEX semantic_user_kind_idx ON semantic_memories(user_id, kind);
CREATE INDEX semantic_confirm_idx ON semantic_memories(user_id)
  WHERE needs_confirmation = TRUE;

-- 变更历史（审计）：不设 FK，保留被删记忆的历史
CREATE TABLE semantic_memory_history (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id  UUID NOT NULL,
  user_id    UUID NOT NULL,
  kind       TEXT NOT NULL,
  key        TEXT NOT NULL,
  old_value  TEXT,
  new_value  TEXT,
  reason     TEXT NOT NULL,   -- new_episode / conflict_resolve / user_edit / user_delete
  changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX semantic_hist_user_idx ON semantic_memory_history(user_id, changed_at DESC);
```

### 6.7 待办、审批、简报（补幂等约束）

```sql
CREATE TABLE todos (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title      TEXT NOT NULL,
  detail     TEXT,
  due_at     TIMESTAMPTZ,
  status     TEXT NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending','done','cancelled')),
  source     TEXT NOT NULL DEFAULT 'chat',
  dedupe_key TEXT,                       -- 工具幂等键（§7.6）
  reminded_at TIMESTAMPTZ,               -- 提醒幂等：已提醒过不再提醒
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, dedupe_key)
);
-- 提醒任务每分钟扫描：部分索引，小且快
CREATE INDEX todos_due_idx ON todos(status, due_at) WHERE status = 'pending';

CREATE TABLE approvals (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  thread_id   TEXT NOT NULL,             -- LangGraph thread = conversation_id
  tool_call   JSONB NOT NULL,            -- 待执行的工具调用（name + args）
  risk_level  SMALLINT NOT NULL CHECK (risk_level IN (1,2)),
  status      TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','approved','rejected','expired','executed')),
  expires_at  TIMESTAMPTZ NOT NULL DEFAULT now() + interval '1 hour',   -- 新增：超时机制落地
  decided_at  TIMESTAMPTZ,
  executed_at TIMESTAMPTZ,
  exec_result JSONB,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX approvals_pending_idx ON approvals(status, expires_at) WHERE status = 'pending';
CREATE INDEX approvals_user_idx ON approvals(user_id, created_at DESC);

CREATE TABLE briefings (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  deliver_date DATE NOT NULL,
  content_md   TEXT NOT NULL,
  sections     JSONB,
  delivered_at TIMESTAMPTZ,
  channel      TEXT,                     -- inapp / webhook
  UNIQUE (user_id, deliver_date)         -- 新增：beat 重跑不重复生成推送
);
```

### 6.8 v1.1 新增表（补齐已承诺但无存储的能力）

```sql
-- 工具调用明细：工具失败率 / 工具选择准确率的唯一数据源
CREATE TABLE tool_invocations (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message_id  UUID REFERENCES messages(id) ON DELETE CASCADE,
  trace_id    TEXT,
  step_id     INT,
  round       INT,                       -- ReAct 轮次
  tool_name   TEXT NOT NULL,
  risk_level  SMALLINT NOT NULL,
  args        JSONB,                     -- 已脱敏
  result_ok   BOOLEAN NOT NULL,
  error_code  TEXT,
  deduplicated BOOLEAN NOT NULL DEFAULT FALSE,
  latency_ms  INT NOT NULL,
  result_chars INT,
  truncated   BOOLEAN NOT NULL DEFAULT FALSE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX tool_inv_user_idx ON tool_invocations(user_id, created_at DESC);
CREATE INDEX tool_inv_name_idx ON tool_invocations(tool_name, result_ok);

-- 用量与配额：日 token 成本、配额计数的持久化（Redis 仅作快速计数）
CREATE TABLE usage_daily (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  usage_date     DATE NOT NULL,
  prompt_tokens  BIGINT NOT NULL DEFAULT 0,
  completion_tokens BIGINT NOT NULL DEFAULT 0,
  request_count  INT NOT NULL DEFAULT 0,
  cost_estimate  NUMERIC(12,4) NOT NULL DEFAULT 0,   -- 按当时价目表估算
  by_feature     JSONB NOT NULL DEFAULT '{}',        -- 成本归因：chat/retrieval/briefing/consolidation
  UNIQUE (user_id, usage_date)
);

-- 审计日志：工具写操作、审批决策、登录事件、管理员操作
CREATE TABLE audit_logs (
  id          BIGSERIAL PRIMARY KEY,
  user_id     UUID,                       -- 不设 FK：保留注销用户的操作痕迹
  actor_type  TEXT NOT NULL CHECK (actor_type IN ('user','worker','admin','system')),
  action      TEXT NOT NULL,              -- tool.invoke / approval.decide / auth.login / memory.write ...
  target      TEXT,
  payload     JSONB,                      -- 已脱敏
  ip          INET,
  user_agent  TEXT,
  trace_id    TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_user_time_idx ON audit_logs(user_id, created_at DESC);
CREATE INDEX audit_action_idx ON audit_logs(action, created_at DESC);

-- 第三方凭据（预留表，当前版本未启用）
CREATE TABLE user_credentials (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider        TEXT NOT NULL,          -- google_calendar / imap_mail / ...
  encrypted_dek   BYTEA NOT NULL,         -- 用 KEK 加密的数据密钥
  nonce           BYTEA NOT NULL,
  ciphertext      BYTEA NOT NULL,         -- AES-256-GCM(凭据 JSON)
  scopes          TEXT[],
  expires_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, provider)
);
```

> **说明**：`tool_invocations` 是 §14 评测与可观测的数据基础——没有它，"工具失败率""工具选择准确率"都无从计算。`usage_daily` 是成本归因（§5.2）的依据。

### 6.9 Agent 检查点

由 `langgraph-checkpoint-postgres` 的迁移脚本生成 `checkpoints` / `checkpoint_writes` / `checkpoint_blobs`。
**注意**：该表不受 RLS 约束（跨租户的框架表），但 `thread_id` 必须使用 `conversation_id`（UUID），业务层保证不跨租户复用。

### 6.10 RLS 策略模板

```sql
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;    -- 关键：不加则表 owner 绕过策略

CREATE POLICY chunks_tenant_isolation ON chunks
  USING (user_id = current_setting('app.user_id', true)::uuid)
  WITH CHECK (user_id = current_setting('app.user_id', true)::uuid);
-- 其余业务表同理

-- 应用侧：必须在事务内 SET LOCAL（连接池复用下 SET 会泄漏）
-- BEGIN; SET LOCAL app.user_id = '<uuid>'; ... COMMIT;
```

---

## 7. Agent 运行时设计（核心）

### 7.1 顶层图（Plan-and-Execute + DAG 并行 + ReAct）

```
START
  → acquire_conversation_lock（会话锁，失败即 409）
  → planner（产出 DAG 计划：Step[] + depends_on）
       ├─ plan 为空 / 纯闲聊 ──────────────→ synthesize
       └─ 一般任务 → execute_dag
                      ├─ 取依赖已满足的 steps（并发上限 3，asyncio.gather）
                      │    step → react_agent（子图）
                      │       ├─ 工具选择 → ToolRegistry.invoke
                      │       │    ├─ risk=0 只读 → 直接执行
                      │       │    ├─ risk=1 写操作 → 执行 + 事后告知
                      │       │    └─ risk=2 不可逆/外发 → interrupt(HITL)
                      │       ├─ 知识类 → retrieval 节点
                      │       └─ 上下文压缩检查（>4 轮触发）
                      ├─ 失败/偏差大 → step_router
                      │    ├─ 可重试 → 重试当前 step（上限 2 次）
                      │    ├─ 需重规划 → 回 planner（replans_left 上限 1）
                      │    └─ 无法完成 → 标记 failed，继续后续可执行 step
                      └─ 全部终止 → synthesize
  → synthesize（汇总各 step 结果，生成带引用答案）
  → groundedness 校验
  → release_lock → END
```

### 7.2 State 定义（v1.1 重写）

**变更要点**：改用 `TypedDict` + 显式 reducer（LangGraph 官方推荐）；每个列表 channel 都有 reducer，避免并行写未定义；补齐关键字段。

```python
from abc import ABC, abstractmethod
from typing import Annotated, TypedDict, Literal
from pydantic import BaseModel, Field, ConfigDict
from langgraph.graph.message import add_messages

# 领域类型在 agent/graph/schemas.py 中定义，此处省略：
#   Chunk, Citation, Artifact, ToolResult, ToolCtx

# ---- reducer ----
def replace(_old, _new):
    """显式覆盖语义。使用 replace 的节点必须返回新对象，禁止原地修改。"""
    return _new

def merge_unique(old, new, key=lambda x: x.chunk_id):
    """按 key 去重合并（用于并行 step 写 citations / retrieved）。"""
    seen, out = set(), []
    for item in (old or []) + (new or []):
        k = key(item)
        if k not in seen:
            seen.add(k); out.append(item)
    return out

def sum_int(old, new):
    return (old or 0) + (new or 0)

def merge_str(old, new):
    """字符串列表去重合并（用于 degraded 原因标记）。"""
    seen, out = set(), []
    for s in (old or []) + (new or []):
        if s not in seen:
            seen.add(s); out.append(s)
    return out

# ---- 数据结构 ----
class Step(BaseModel):
    id: int
    description: str
    tool_hint: str | None = None
    depends_on: list[int] = Field(default_factory=list)   # DAG 依赖
    status: Literal["pending","running","done","failed","blocked","skipped"] = "pending"
    result: str = ""
    attempts: int = 0
    error: str | None = None

class AgentState(TypedDict):
    # 会话上下文
    messages:       Annotated[list, add_messages]
    user_id:        str
    conversation_id: str
    timezone:       str
    mode:           str                      # chat / agent / research

    # 计划与执行
    plan:           Annotated[list[Step], replace]
    replans_left:   int
    max_parallel:   int

    # 检索与引用
    system_context: Annotated[str, replace]        # 系统级上下文（检索注入）
    retrieved:      Annotated[list[Chunk], merge_unique]
    citations:      Annotated[list[Citation], merge_unique]

    # 预算控制（v1.1 新增）
    tokens_used:    Annotated[int, sum_int]
    token_budget:   int
    rounds_used:    int
    compress_count: int

    # HITL
    approval_id:        str | None
    pending_tool_call:  dict | None

    # 降级与错误（v1.1 新增）
    degraded:       Annotated[list[str], merge_str]  # 降级原因列表
    error:          str | None
    artifacts:      Annotated[list[Artifact], replace]          # 生成的报告/文件
    trace_id:       str
```

**必须遵守的编码规范（写进 `CONTRIBUTING.md`）**：
1. **禁止原地修改 state 中的列表/对象**（`state["plan"][0]["status"] = "done"` 不会触发 channel 更新——LangGraph 靠节点**返回的字典**更新）。必须返回新列表。
2. 无 reducer 的标量 channel（如 `rounds_used: int`）**只能由单个节点在单个超步内更新**；需要多节点并发写时必须给 reducer。
3. 新增 channel 时必须显式选择 reducer，并在 PR 中说明选择理由。

> **面试要点**：这一条是真实的 LangGraph footgun，主动讲"我因为原地修改导致状态不更新，排查了半天，最后改成 reducer 模式"，比任何概念解释都有说服力。

### 7.3 ReAct 子图与工具调用

- 每轮：LLM 决策（function calling）→ 命中工具则经 `ToolRegistry.invoke` 执行 → Observation 回灌。
- **终止条件（四选一，全部必须显式判断）**：
  1. 无新工具调用（模型给出最终答案）
  2. 轮次超过 `MAX_ROUNDS=8`
  3. token 预算达到阈值（见 §7.8）
  4. 同一工具以相同参数重复调用（循环检测，防死循环）

- **工具风险分级（本地策略定义，不信任工具自述）**：
  | 等级 | 类型 | 示例 | 处理 |
  |------|------|------|------|
  | L0 | 只读 | search / retrieve / math | 自动执行 |
  | L1 | 写操作 | 创建 todo / 保存邮件草稿 | 执行 + 事后告知（含幂等键） |
  | L2 | 不可逆 / 外发 | 发送邮件 / 删除文件 / 邀请他人 | **interrupt → approvals → 人工审批** |

- **参数级风险策略（v1.1 新增）**：纯 per-tool 分级不够。同一个 `send_email`，发给自己和群发给 50 个外部域名风险不同。
  → `ToolMeta.args_policy` 支持按参数动态升级风险，例如：`send_email` 收件人数 > 3 或收件域名不在白名单 → 自动升级为 L2；`filesystem.delete` 路径不在工作目录 → 直接拒绝。

- **Checkpointer**：`PostgresSaver`，`thread_id = conversation_id`。

### 7.4 会话级并发控制（v1.1 新增）

**问题**：`thread_id = conversation_id` 意味着同一会话的执行状态是**串行独占**的。若不定策略，并发请求会导致状态互相覆盖。

**设计**：

| 场景 | 行为 |
|------|------|
| 上一轮未结束时又发消息 | Redis 锁（`SET conversation_lock:{cid} NX PX 300000`）获取失败 → 返回 `409 CONFLICT` + `Retry-After`；前端本就在 loading 态，体验自然 |
| "重新生成"连点 | 同锁保护；未获取到锁则忽略重复请求（前端按钮同时置灰） |
| 审批回调与新消息竞争 | 审批回调走同一把锁（它是同 thread 的续跑），保证串行 |
| 长任务（深度研究）超 5 分钟 | 锁 TTL 到期前由持有者续期（`watchdog`）；任务结束显式释放 |

**配套**：`messages.seq` 保证顺序；`UNIQUE(conversation_id, seq)` 由 DB 兜底防重。

### 7.5 工具协议与治理层（v1.1 重写）

**v1.0 的问题**：把 `async def arun` 塞进 `BaseModel`，让数据校验模型承载执行逻辑；且超时/重试/幂等/风险拦截散落在各工具中。

**v1.1 拆分为"元数据 + 执行体 + 注册表"三层**：

```python
# ---- 元数据（Pydantic 只负责校验与描述）----
class ArgPolicy(BaseModel):
    """参数级风险升级规则。"""
    escalate_to_l2_if: str | None = None    # 表达式，如 "len(to) > 3 or any(external(d) for d in to)"
    deny_if: str | None = None

class ToolMeta(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str
    description: str
    args_schema: type[BaseModel]
    risk_level: Literal[0, 1, 2] = 2        # 默认最高风险：未声明即保守
    idempotent: bool = False                # 默认非幂等：强制提供 dedupe 逻辑
    timeout_s: float = 15.0
    max_result_chars: int = 4000
    max_concurrency: int = 3
    args_policy: ArgPolicy | None = None
    source: Literal["builtin", "mcp"] = "builtin"
    mcp_server: str | None = None

# ---- 执行体（ABC 强制实现）----
class BaseTool(ABC):
    meta: ToolMeta
    @abstractmethod
    async def arun(self, ctx: ToolCtx, **kwargs) -> ToolResult: ...

class ToolResult(BaseModel):
    ok: bool
    content: str
    full_ref: str | None = None      # 超长内容的存储引用（§7.8）
    truncated: bool = False
    error_code: str | None = None
    latency_ms: int = 0

# ---- 注册表：横切关注点唯一收敛点 ----
class ToolRegistry:
    def register(self, tool: BaseTool) -> None: ...
    def get(self, name: str) -> BaseTool: ...      # 未注册 → ToolNotAllowed（fail-closed）

    async def invoke(self, name: str, args: dict, ctx: ToolCtx) -> ToolResult:
        """统一处理：策略校验 → 风险分级(含参数级) → 幂等去重 → 并发限流
        → 超时 → 重试 → 结果截断 → 埋点(tool_invocations) → 审计(audit_logs)"""
```

**收效**：新增工具只需实现 `arun` + 声明 `meta`，其余（超时、幂等、埋点、审计、风险拦截）**零成本继承**。这是"横切关注点收敛"的教科书式应用，也是面试中很好的架构表达素材。

**工具结果截断**：超过 `max_result_chars` 的部分不丢弃，写入对象存储/表并在 `full_ref` 返回引用 ID；模型看到"结果过长，已截断"提示，可在后续轮次按 `full_ref` 二次取用。

### 7.6 幂等与重试设计（v1.1 新增）

**问题**：系统同时存在**三个重试源**——前端网络重试、SSE 断线重连、Celery 任务重试（指数退避 3 次）。而工具是有副作用的。没有幂等，"发送邮件"重试两次就是发两封。

**三层幂等**：

| 层 | 机制 | 粒度 |
|----|------|------|
| **API 层** | 请求头 `Idempotency-Key`（前端为每次用户输入生成 UUID）；服务端 Redis 存 `key → {message_id, status}`，TTL 24h；重复请求直接返回已有 message_id 的流 | 一次用户请求 |
| **工具层** | `dedupe_key = sha256(thread_id + step_id + round + tool_name + canonical_json(args))`，写 Redis（TTL 24h）并在 `tool_invocations.deduplicated` 标记；命中则返回上次结果 | 一次工具调用 |
| **存储层** | `todos UNIQUE(user_id, dedupe_key)`、`briefings UNIQUE(user_id, deliver_date)`、`documents UNIQUE(user_id, content_hash)`、`messages UNIQUE(conversation_id, seq)` | DB 兜底 |

**`canonical_json`**：参数 JSON 必须规范化（键排序、去空白、数值归一），否则 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 会被视为不同调用。

**Celery 侧**：入库任务按 `sha256(document_id + ord + content)` 计算内容指纹用于**变更检测**，并由 `chunks` 表的 `UNIQUE(document_id, chunk_type, ord)` 约束在 DB 层兜底幂等——重跑时走 upsert，不产生重复块（v1 的 `batch_build_kb.py` 已有断点续传/去重思路，v1.1 把它落实为 DDL 约束）。

**重试策略**：只对**幂等工具**自动重试；非幂等工具（`idempotent=False`）失败后**不自动重试**，转为向用户报告 + 建议手动重试。

### 7.7 流式生命周期与断线恢复（v1.1 新增）

**问题**：用户发完长任务后关掉页面，会怎样？SSE 断了，答案和工具调用消失吗？

**设计：执行与传输解耦**

1. **`POST /api/v1/chat` 语义 = 投递任务**，返回 `{message_id, stream_url}`。Agent 执行由 Runtime 独立推进，**不绑定 HTTP 连接生命周期**。
2. **assistant 消息在流开始时即落库**，`status='streaming'`；流结束更新为 `completed`；中断则为 `interrupted` 并保留已生成内容。→ **刷新页面答案永不消失。**
3. **重连语义**：事件流不重放；浏览器重开/断线后以已落库内容为准（拉取 `messages` 全文），后续增量继续订阅。
4. **取消语义**：`POST /api/v1/chat/{message_id}/cancel` → 取消 asyncio task → 消息标记 `cancelled` → **不计入评测集**。
5. **"重新生成"** = 新建一轮（新 seq），旧消息保留并置 `superseded_by`，不原地覆盖。

### 7.8 上下文与 token 预算管理（v1.1 新增）

**问题**：ReAct 多轮 + 工具返回大结果，很容易撑爆上下文窗口；v1.0 只写了一句"预算超限"，没有行为定义。

**三层控制**：

1. **工具结果截断**（§7.5）：单次结果上限 `max_result_chars`，超出存 `full_ref`。
2. **滚动压缩**：ReAct 轮次 > 4 后，把前 N 轮的 (思考 / 工具调用 / 观察) 压缩为一条结构化摘要——
   `{工具名, 关键参数, 结论摘要, 产生的引用 id}`，原始内容移出上下文。压缩本身是一次 LLM 调用，要计入预算。
3. **预算显式化与降级**：
   | 阈值 | 行为 |
   |------|------|
   | 达到 80% | 停止开启新的工具调用，直接进入 `synthesize` |
   | 达到 100% | 用已得结果生成"**部分答案**"，UI 明确标注"因预算限制未能完成全部步骤" |
   | 单次对话硬上限 | 由 `DAILY_TOKEN_BUDGET` 与单轮预算共同约束 |

4. **计划步数上限**：`plan` 长度 ≤ 6 步，超出要求 planner 合并子任务或向用户澄清（避免"计划爆炸"）。

---

## 8. RAG 管线设计

### 8.1 入库（Celery 异步，独立 `ingest` 队列）

```
文件
 → 上传层校验（大小/MIME+magic bytes/配额/文件名净化，见 §13.4）
 → content_hash 去重（同用户同文件直接复用，省 embedding 成本）
 → 解析路由：
     文本型 PDF        → PyMuPDF（快）
     含表格/复杂版式   → unstructured / MinerU（慢但准）
     扫描件/图片       → 告警提示（OCR 需手动触发，计入配额）
     公式             → 提取为 LaTeX 并保留（供前端 KaTeX 渲染）
 → 去噪：按页重复度识别并移除页眉/页脚/参考文献
 → 父子分块（子 256 token / 父 1024 token / 重叠 10%）
 → jieba 分词 → content_tokens
 → 批量 embedding（批次重试、失败记录、幂等 chunk_id）
 → 同事务写 chunks + document.status='ready'
```

**解析可追溯**：`documents.parser_used` 记录所用解析器，便于把入库质量问题（缺块/乱码）定位到解析环节。

### 8.2 在线检索

```
query（查询侧先经 jieba 分词，供关键词路使用）
 → 并行双路召回（每路 top 200，为 ADR-2 的过滤召回损失留余量）：
      · 向量路：embedding cosine (<=>) + user_id 过滤 + hnsw.iterative_scan
      · 关键词路：tsv (ts_rank_cd)
 → RRF 融合（k=60）去重 → 候选 ~50
 → Reranker（qwen3.7-text-rerank，cross-encoder 类）精排 → top 6
 → 父块回溯（按 parent_id 取父块，相邻父块去重）→ 注入上下文（附 chunk_id）
```

**延迟预算**：

| 阶段 | 目标 |
|------|------|
| 检索段（召回+融合+精排+回溯） | P95 < 300ms |
| 端到端首 token | P95 < 2.5s |

### 8.3 降级矩阵（v1.1 新增，全章最重要的补充）

**设计原则：降级必须显式** —— 任何跳过精排或护栏的路径，**UI 必须打出可见标记**。

| 环节 | 超时 | 失败降级 | 用户可见影响 |
|------|------|---------|------------|
| 向量召回 | 500ms | 仅走关键词路，标记 `degraded:vector` | 语义相近但用词不同的内容可能漏 |
| 关键词召回 | 500ms | 仅走向量路，标记 `degraded:keyword` | 专名/术语精确匹配下降 |
| RRF 融合 | — | 总是可用（纯内存计算） | 无 |
| **Rerank** | 800ms | 退化为 RRF 顺序，标记 `degraded:rerank`，**UI 显示"未精排"角标** | 排序质量下降 |
| 父块回溯 | — | 退化为直接用子块 | 上下文略少 |
| Groundedness 校验 | 1s | 跳过校验，**UI 强制显示"未校验"角标** | **必须让用户知道** |
| MCP 工具 server | 15s | 该工具不可用，返回结构化错误给 Agent 让它换路径 | 该能力暂不可用 |

**实现**：所有降级写入 `AgentState.degraded` → 落 `messages.degraded` → 通过 SSE `degraded` 事件推给前端 → UI 渲染角标。
**验收方式**：用环境变量开关（feature flag）模拟依赖故障，不依赖真实故障注入。

> **面试要点**：能说出"降级必须显式，偷偷降级是产品级事故"，是**产品意识**的表现，而不只是工程意识。

### 8.4 引用与 groundedness

- 答案以上标 `[1][2]` 标注；前端悬浮显示来源文档标题/段落，可跳转原文。
- groundedness：NLI 风格逐句核验。**无出处句子比例 > 20%** 时触发一次"针对性补充检索 + 重写"；仍不达标则在 UI 标黄"该结论缺少资料支撑"。
- 引用存 `messages.citations` JSONB，结构：`{n, chunk_id, document_id, title, snippet, rerank_score}`。

---

## 11. API 契约（v1）

### 11.1 通用约定

- 统一响应：成功 `{data, trace_id}`；失败 `{error: {code, message, detail}}`。
- **错误码字典**：扩展自 v1 的"错误码→友好提示"映射结构（见附录 C），新增 `CONFLICT`、`QUOTA_EXCEEDED`、`IDEMPOTENT_REPLAY`、`RETRIEVAL_DEGRADED`。
- **分页**：所有列表接口统一 `?cursor=&limit=`（游标分页，适配实时写入的表，避免 offset 漂移）。
- **幂等**：所有 `POST`/`PATCH`/`DELETE` 支持 `Idempotency-Key` 头（§7.6）。
- **健康检查分层**：`/health`（存活，不查依赖）与 `/ready`（就绪，检查 DB/Redis）分离。
- **配额响应**：`429` + `Retry-After` 头 + `QUOTA_EXCEEDED`。

### 11.2 接口清单

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/auth/register` `/login` `/refresh` `/logout` | JWT（access 30min + refresh 7d，**refresh 轮转**） |
| POST | `/api/v1/chat` | **投递任务**，返回 `{message_id, stream_url}` |
| GET | `/api/v1/chat/{message_id}/stream` | **SSE 订阅**（事件流不重放，重连以落库内容为准） |
| POST | `/api/v1/chat/{message_id}/cancel` | 取消生成（§7.7） |
| GET | `/api/v1/conversations` `/conversations/{id}/messages` | 历史（游标分页） |
| POST | `/api/v1/kb/documents` | 创建文档（multipart 上传，异步入库） |
| GET | `/api/v1/kb/documents/{id}/status` | 入库状态轮询 |
| POST | `/api/v1/retrieval/search` | 独立混合检索（调试台用，可看多路分数与 RRF/rerank 明细） |
| GET | `/api/v1/approvals`、`POST /{id}/approve` `/{id}/reject` | HITL |

### 11.3 SSE 事件协议（增加 seq 与重连）

```
event: token        data: {"delta":"..."}
event: tool_start   data: {"tool":"web_search","args":{...},"risk":0}
event: tool_end     data: {"tool":"...","result_excerpt":"...","truncated":false}
event: approval     data: {"approval_id":"...","action":{...}}
event: degraded     data: {"reason":"rerank","message":"本轮未启用精排"}
event: citation     data: {"items":[{n,chunk_id,title,snippet,score}]}
event: done         data: {"message_id":"...","usage":{...},"trace_id":"..."}
event: error        data: {"code":"...","message":"..."}
```

**说明**：事件流即时推送、不提供断线重放；重连后以已落库内容为准。前端用 `fetch` + ReadableStream 消费 SSE（可携带 `Authorization` 头，无需 EventSource 的 query token 方案）。

---

## 12. 前端设计（Next.js）

### 12.1 页面

| 路径 | 功能 |
|------|------|
| `/login` `/register` | 认证 |
| `/chat` | 主对话：工具过程折叠卡片、引用脚注悬浮卡、审批卡片、**降级角标**、token/耗时徽标、停止生成、重新生成 |
| `/kb` | 文档列表/上传/入库进度/删除；**检索调试台**（多路召回分数 + RRF + rerank 明细） |
| `/settings` | 主题等基础设置 |

### 12.2 技术要点

- **原生 `fetch` + ReadableStream 消费 SSE**（M0 起）：不引入额外 SDK，事件解析逻辑与后端协议一一对应。
- **Markdown 渲染必须配 sanitize（防 XSS）**：检索内容可能被注入 HTML。
- token 拦截刷新（401 自动 refresh 一次）；shadcn/ui + Tailwind；深色模式。
- **错误/空/加载三态**设计，不留白屏。

---

## 13. 安全与护栏

### 13.1 认证授权

JWT + bcrypt；refresh token 轮转与重放检测（§6.3）；所有路由注入租户上下文；**RLS 兜底**（必须 `FORCE ROW LEVEL SECURITY` + 事务内 `SET LOCAL`，见 ADR-9）；`role` 控制 admin 路由。

**明确说明**：access token 有效期 30min 内**无法撤销**（无状态 JWT 的固有代价）。若需即时撤销，需引入黑名单（Redis）——本项目不做，因为 30min 窗口风险可接受，且登出时 refresh token 已失效。

### 13.2 PII 与脱敏

入日志、入第三方模型、**入 Langfuse trace** 前做正则 + NER 脱敏（手机/身份证/邮箱/密钥）。
**v1.1 强调**：trace 通道最容易漏——SDK 默认会把 input/output 全量上报。必须配置 redaction 回调，并做**自动化测试**断言 trace 载荷中不含测试用的敏感串。

### 13.3 工具风险

三级风险分级 + **参数级升级策略**（§7.3）+ 审批（M4/M5 落地）。

### 13.4 上传与解析安全（v1.1 新增）

文件上传是 RCE 与 DoS 的经典入口，需分层防护。

| 层 | 措施 |
|----|------|
| 上传层 | 大小上限 50MB；MIME + **magic bytes 双重校验**（含后缀与内容一致性）；每用户文档数配额；文件名净化（防路径穿越） |
| 解析层 | 解析在 **Celery Worker 子进程**中执行（与 API 隔离）；**限制 zip 解压比**（DOCX 是 zip，防 zip bomb）；解析超时；页数上限 |

### 13.5 沙箱

RestrictedPython 白名单 + **OS 级资源限制**（`resource.setrlimit` 限制 CPU/内存/文件大小 + 超时 + 禁止 fork）+ 子进程隔离。
**诚实说明局限**：RestrictedPython ≠ OS 隔离。进程级限制能防住死循环与内存炸弹，但**不能防住容器逃逸类攻击**——本项目不处理该威胁模型（无多租户代码执行需求）。

### 13.6 MCP 工具治理（v1.1 新增）

见 ADR-4。核心要点：

1. **本地策略注册表，fail-closed**：未注册工具默认拒绝；风险等级来自本地定义，**不信任工具自述**。
2. **描述净化**：工具名/描述会进入 function calling schema，是注入载体 → 剥离指令性语句、限制长度（≤500 字符）、入库前人工 review 一次。
3. **Server 白名单 + 版本锁定**：禁用自动发现；server 升级需 review。
4. **权限最小化**：每个 server 只获得完成其职责所需的最小权限与目录。
5. **生命周期**：健康检查 + 调用超时 + 崩溃自动重启 + **重启后校验工具列表是否变化**（变化则告警，可能是供应链攻击）。

### 13.7 输出与合规

groundedness 校验；**前端 Markdown 必须 sanitize**（防通过检索内容注入 XSS）。

### 13.8 审计

`audit_logs` 记录：工具写操作（含 L1/L2）、审批决策、登录事件、管理员操作。
**注意**：审计 payload **必须脱敏**——它是要长期保留的，不能成为 PII 泄漏源。

### 13.9 其他

- **CORS / CSRF**：SSE 与 cookie 场景下需校验 `Origin`。
- **数据保留**：用户注销 → 级联删除所有业务数据 + 对象存储文件；`audit_logs` 保留（`user_id` 不设 FK，见 §6.8）。
- **日志**：禁止记录完整的用户输入与模型输出（只记长度与哈希），避免日志成为 PII 泄漏源。

---

## 14. 可观测性与评测

### 14.1 观测三支柱

**Trace（Langfuse）**：每次 chat 一条 trace，planner / 工具 / 检索 / LLM / rerank 各为 span，含 input/output/token/cost/user。**必配 PII redaction**（§13.2）。

**Metrics**：关键指标从 trace 与 `tool_invocations` 汇总：分段延迟（首 token / 检索段 / rerank）、检索过滤有效召回率、rerank 命中率、工具失败率、降级触发率、token 成本。
**SLO**：见 §5.3。

**Logs（structlog）**：JSON 化，`trace_id` 贯穿 FastAPI → Worker → MCP server。

### 14.2 评测方法论（v1.1 重写，这是本版最重要的方法论修正）

#### 14.2.1 指标分层

| 层 | 指标 | 来源 |
|----|------|------|
| **检索层** | Recall@k、MRR、nDCG@10 | **v2 自建**（v1 无评测脚本） |
| **轨迹层** | 工具选择准确率、计划完成率、平均轮次、平均成本、降级率 | `tool_invocations` + trace |

**为什么分层**：只有分层指标才能回答"效果变好是因为检索变好还是生成变好"——这是**归因分析**的前提，也是面试中最有说服力的部分。

#### 14.2.2 golden set

规模 **50 条起步**（知识问答为主），在 M7 补充 **10–20 条真实语料题**（从实际上传的文档出题）——合成语料区分度不足，真实语料才能测出管线差异。

**构造方法**：LLM 从语料生成候选问题 + 人工校验与标注（标准答案 / 应命中文档 / 应命中块）。

#### 14.2.3 噪声地板（关键）

**问题**：非确定性指标（检索排序受 embedding 服务与数据分布影响）存在波动。在未测波动范围前设"回退 N 个点即阻断合并"，会导致门禁频繁假红灯 → 被忽略 → 门禁失效。

**做法**：
1. 同一份代码、同一批样本，`temperature=0`、固定 `seed`、固定 prompt 版本，**重复评测 5 次**。
2. 得到每项指标的 `mean ± σ`，作为解读版本差异的误差基准（**差异 < 2σ 视为噪声**）。
3. 把噪声地板实验本身写进 README。

#### 14.2.4 版本对比表

| 版本 | Recall@10 | nDCG@10 | 平均延迟 | 平均成本 |
|------|-----------|---------|---------|---------|
| v1 单路向量（baseline，同集重跑） | — | — | — | — |
| + 混合检索（关键词 + RRF） | — | — | — | — |
| + Rerank | — | — | — | — |

**归因要求**：除总分外，必须拆出"X 个点来自混合召回，Y 个点来自 Rerank"——**归因才是这张表的灵魂**。

#### 14.2.5 CI 与评测的关系

- CI：`ruff` + `mypy` + `pytest`（单元与安全用例），不跑评测 job（评测在本地按需执行）。
- 评测结果以对比表形式沉淀为文档；差异解读以噪声地板为参照。

### 14.3 feature flag（v1.1 新增）

**问题**：v1.0 要求做"baseline / +hybrid / +rerank"多版本对比，但串行改代码 + 每次重跑评测既慢又不可复现。

**方案**：轻量 feature flag（配置文件 + Redis 覆写，无需引入 Unleash）：
`retrieval.hybrid.enabled`、`retrieval.rerank.enabled`。

**收益**：评测时对**同一批 query 用不同 flag 组合跑**，一次拿到对比表；降级验收也用同一开关模拟依赖故障（§8.3）。

---

## 15. 部署与 CI/CD

### 15.1 docker-compose 服务

| 服务 | 说明 |
|------|------|
| `postgres` | pgvector 镜像（≥ 0.8，需 iterative_scan） |
| `redis` | 会话锁 + 幂等 + Celery broker |
| `caddy` | **反向代理 + 自动 HTTPS**（v1.0 缺失，见下） |
| `api` | FastAPI |
| `worker` | Celery worker，**独立 `ingest` 队列**，文档入库（见 ADR-5） |
| `web` | Next.js standalone |

**Caddy 配置关键点（v1.0 缺失，这是 SSE 上线后必踩的坑）**：

```caddy
agent.example.com {
    reverse_proxy /api/v1/chat/* api:8000 {
        flush_interval -1          # 关键：立即刷新，禁用缓冲，否则流式失效
    }
    reverse_proxy api:8000
    reverse_proxy / web:3000
}
```
> **若用 Nginx**，等价配置为 `proxy_buffering off; proxy_cache off; proxy_read_timeout 300s; chunked_transfer_encoding on;`。
> **面试价值**：能说出"SSE 必须关闭反代缓冲，否则用户看到的是转圈 30 秒后一次性吐全文"，一个细节就证明你真的部署过。

**Alembic 迁移（v1.0 有隐患）**：v1.0 写"容器启动时自动 `upgrade head`"。多副本并发启动会**并发执行迁移**导致冲突。
→ **方案**：迁移由独立的 `migrate` job/容器执行（`depends_on: service_completed_successfully`），或在迁移前用 PostgreSQL advisory lock 串行化。

**其他**：健康检查与 `depends_on` 齐备；`mem_limit` 限制关键服务内存；`.env.example` 提供全量配置；**生产 secrets 通过环境注入，不进镜像、不进仓库**。

### 15.2 GitHub Actions

- **PR**：`ruff` + `mypy` + `pytest`（单元用例；安全集成用例依赖本地栈，在本地收尾时人工跑）。
- **main**：构建镜像、打 tag（可选部署到单机 VPS：`docker compose pull` + `migrate` job + `up`）。

---

## 16. 测试策略（v1.1 新增）

v1.0 只有一句"核心路径必须有 pytest"，缺少分层与门槛。

| 层 | 范围 | 要求 |
|----|------|------|
| **单元测试** | 分块器、RRF 融合、引用解析、风险分级策略、PII 脱敏、上传校验 | 覆盖率 ≥ 80%，纯函数，毫秒级 |
| **集成测试** | Agent 图（**用 fake LLM，不真调模型**）、检索管线（固定小语料） | CI 必跑 |
| **契约测试** | **SSE 事件协议**（防止前后端协议漂移）、工具 `args_schema` 与 `ToolRegistry` 的匹配 | **这个点很亮，很多项目想不到** |
| **安全测试** | 跨租户越权（A 读 B 数据 → 404/空）、**trace 载荷不含敏感串** | CI 必跑 |

---

## 17. 里程碑与验收（施工依据）

> ⚠️ **本节已被取代。** 施工范围、任务分解与验收标准以项目根目录 `项目实施计划.md` 为准。


---

## 18. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 范围膨胀 | 烂尾 | 严守 §1.3 非目标；新想法进 backlog 不插队；每周五 tag + 录屏 |
| MCP 生态不稳定 / 协议演进 | 返工 | 治理层与工具实现解耦；固定 server 版本；协议变更只改适配器 |
| 云端成本失控 | 烧钱 | 日配额 + 成本归因（§5.2） |
| Windows 本地开发坑 | 环境问题 | 全部服务容器化；Python 只在 venv/devcontainer 跑 |
| 嵌入模型维度锁定 | 换模型报错 | Embedding 与 LLM 配置分离 + 启动自检 + 重嵌入脚本 + UI 警告 |
| 一人项目测试覆盖不足 | 质量塌方 | §16 分层测试；跨租户越权与契约测试为 CI 必过项 |
| 单点故障（只有一个人维护） | 项目死亡 | 文档化优先（ADR + README）；关键决策留痕，降低接手成本 |

---

## 19. 简历呈现预演（项目完成后的 bullet 草稿）

1. **Agent 运行时**：设计并实现基于 LangGraph 的 **Plan-and-Execute 多 Agent 运行时**——planner 产出 **DAG 计划并并行执行**（深度研究类任务耗时从 Xs 降至 Ys）、ReAct 工具循环（预算受控 + 上下文滚动压缩）、Postgres Checkpointer **进程崩溃后中断恢复**、**三级工具风险 + 参数级升级策略**的人工审批闭环。
2. **工具层**：基于 **MCP 协议**自建 sandbox / todo / search 工具，并实现**客户端治理层**（本地策略注册表 fail-closed、工具描述净化、白名单、健康检查）；将超时/重试/幂等/埋点收敛到统一 `ToolRegistry`，新增工具零成本继承横切能力。
3. **生产级 RAG**：父子分块 +（pgvector 向量 + PostgreSQL 全文检索）混合检索经 RRF 融合与 BGE Reranker 精排；**检索层评测集（Recall@k / MRR / nDCG@10）+ 噪声地板**驱动，nDCG@10 由 X 提升至 Y，并完成**逐项归因**（混合召回 +A 点 / 精排 +B 点）。
4. **多租户向量检索**：识别并解决 pgvector 在多租户过滤下 **HNSW 召回坍塌**问题（`hnsw.iterative_scan` 迭代扫描），过滤检索 Recall@10 由 X% 恢复至 Y%。
5. **工程健壮性**：FastAPI async + SSE 流式 + JWT 多租户（PG RLS，跨租户 404 隔离）+ **幂等设计（Idempotency-Key + 重放检测）** + **依赖降级矩阵（显式角标）**；Langfuse 全链路追踪与 PII 脱敏，月度成本约 ¥X。
6. **交付与验证**：Docker Compose 一键部署（6 服务编排）+ GitHub Actions（lint/mypy/test）+ 3 分钟 demo 视频。

> **注意**：X/Y 等指标用实测数据填写，**禁止编造**。面试官会要求看原始数据。
> **取舍型 bullet（建议保留一条）**：例如"在 pgvector / Milvus / Chroma 之间评估后选择 pgvector，以运维复杂度换开发效率；并明确承认放弃 BM25 而使用 ts_rank_cd 的代价"——**展示判断力比展示技术栈更值钱。**

---

## 20. 对外交付物清单（v1.1 新增）

求职载体本身需要被设计，不能只有代码。

| 交付物 | 内容 | 优先级 |
|--------|------|--------|
| **README（三层）** | ① 一句话定位 + GIF 演示 ② 架构图 + 快速开始（30 分钟部署） ③ 深入链接 | P0 |
| **ADR 目录** | `docs/adr/0001-*.md ...`，每条含背景/决策/替代方案/代价 | P0 |
| **评测报告** | 独立文档：方法论 + 噪声地板 + 对比表 + 归因 | P0 |
| **Demo 视频（3 分钟）** | 分镜：问答带引用 → 工具调用 + 审批 → 中断恢复 → **降级角标** | P0 |
| **架构图 / 时序图** | 组件图、SSE 时序图、HITL 审批时序图 | P1 |

---

## 附录 A：旧资产迁移映射（v1.1 修正版）

v1.0 的映射表未明确旧检索实现与各数据处理文件的去向，本版逐文件补齐。

| 旧文件 / 资产 | v1.1 去向 | 说明 |
|--------------|----------|------|
| `tools/code_sandbox.py` | `mcp_servers/sandbox_server/` | MCP 化 + 子进程 + **OS 资源限制**（§13.5） |
| `agents/router_agent.py` | `agent/graph/nodes/planner.py` 的前置意图信号 | 弱化为 planner 的一次轻量分类调用 |
| `agents/qa_exercise_agent.py` | `agent/graph/nodes/study_subgraph.py` | 保留五步讲解 + 沙箱 |
| `agents/retrieve_agent.py` | **拆分**：检索逻辑 → `retrieval/`；material/literature 双分支的**文献格式化** → 保留为工具 | 检索与格式化职责分离 |
| `tools/vector_search.py`（Chroma 单路，含文件锁重试） | **`evals/baseline/v1_chroma_retriever.py`（保留为评测 baseline）** + v2 新建 `retrieval/hybrid.py` | **v1 无混合检索、无评测**；保留最小实现是为了让 §14.2 对比表有可复现的对照物 |
| `agents/base_agent.py` | `agent/provider/` | 抽为 `LLMProvider`，**LLM 与 Embedding 开关分离**（ADR-8） |
| `data_process/doc_parser.py` `text_splitter.py` | `retrieval/parser.py` `splitter.py` | 增强为解析路由 + 去噪 + 公式保留（§8.1） |
| `data_process/dataset_clean.py` | `scripts/dataset_clean.py` | 离线语料构建工具，保留 |
| `data_process/batch_build_kb.py` | `worker/tasks/ingest.py` | **断点续传与去重逻辑保留**，改造为 Celery `ingest` 队列任务 |
| `graph/workflow.py` | `agent/graph/builder.py` 重写 | 保留其超时包装与单例注册表思路 |
| `api/main.py` | `apps/api/` 拆分路由 | 统一错误码字典一并迁移（附录 C） |
| `api/schemas.py` 错误码映射 | `apps/api/core/errors.py` | v1 仅 8 个错误码且多为 Chroma/Ollama 专属，**作为起点扩展**为完整字典（附录 C） |
| `frontend/*`（Streamlit） | Next.js 重写；Streamlit 可选留作 `/debug` | |
| `CLOUD_MODEL_GUIDE.md` | **改写并归档**，修正"切换模式不影响已有数据"的表述 | 见 ADR-8 |
| `.trae/rules/*.md` | `docs/archive/v1-rules/` | 废止并留痕（§0.2） |
| 旧向量库数据 | **废弃，不迁移**（v1 为课程样例数据） | 需与 README 中的规模描述统一口径 |

---

## 附录 B：环境变量规划（.env.example 摘要）

```ini
# ---- 数据库与缓存 ----
DATABASE_URL=postgresql+asyncpg://app_api:***@postgres:5432/agent
DATABASE_URL_WORKER=postgresql+asyncpg://app_worker:***@postgres:5432/agent   # BYPASSRLS
DATABASE_URL_MIGRATE=postgresql+asyncpg://app_owner:***@postgres:5432/agent
REDIS_URL=redis://redis:6379/0

# ---- 模型（全程云端，见 ADR-8）----
# LLM：变更无副作用，随时可改
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=***
LLM_MODEL=                      # 填控制台确认的模型名
LLM_SMALL_MODEL=                # 小模型路由（意图分类 / query 改写）

# Embedding：变更 = 全量重嵌入，启动自检会直接拦截
EMBED_BASE_URL=                 # DashScope 兼容模式地址
EMBED_API_KEY=***
EMBED_MODEL=                    # 填控制台确认的模型名
EMBED_DIM=1024                  # 与 DDL 锁定，启动自检

# Reranker：阈值需按模型标定（ADR-8 第 4 条）
RERANK_BASE_URL=
RERANK_API_KEY=
RERANK_MODEL=qwen3.7-text-rerank   # 阈值必须按本模型标定（ADR-8 第 4 条）

# ---- 鉴权与加密 ----
JWT_SECRET=***
ACCESS_TTL_MIN=30
REFRESH_TTL_DAYS=7

# ---- 观测 ----
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=***
LANGFUSE_SECRET_KEY=***
LANGFUSE_REDACT_PII=true        # 必须为 true

# ---- MCP ----
MCP_ALLOWED_SERVERS=sandbox,todo,web-search
MCP_SANDBOX_CMD=***

# ---- 检索 ----
HYBRID_TOP_K=200
RRF_K=60
RERANK_TOP_N=6
ITERATIVE_SCAN=relaxed_order
```

---

## 附录 C：错误码字典（迁移自 v1 资产）

| 错误码 | 含义 | 处置 |
|--------|------|------|
| `AUTH_FAILED` | 认证失败 | 重新登录 |
| `TOKEN_EXPIRED` / `TOKEN_REUSE_DETECTED` | 令牌过期 / **检测到重放** | 重新登录；后者需提示改密 |
| `FORBIDDEN` | 越权 | 前端跳首页 |
| `CONFLICT` | 会话正在处理中（§7.4） | 提示稍后重试，`Retry-After` |
| `VALIDATION` | 参数校验失败 | 展示字段级错误 |
| `QUOTA_EXCEEDED` | 配额耗尽 | 展示配额与重置时间 |
| `IDEMPOTENT_REPLAY` | 重复请求（已返回原结果） | 透明处理，前端无感 |
| `LLM_OFFLINE` / `LLM_TIMEOUT` | 模型服务不可用 | 自动重试/降级；仍失败则提示稍后重试 |
| `EMBED_DIM_MISMATCH` | 嵌入维度与库不一致 | **拒绝启动**，提示重嵌入 |
| `RETRIEVAL_DEGRADED` | 检索降级（非错误） | UI 显示角标，不阻断 |
| `RERANK_UNAVAILABLE` | 精排不可用 | 降级为 RRF 顺序 + 角标 |
| `TOOL_NOT_ALLOWED` | 工具未在策略注册表（fail-closed） | 记录审计，告知能力不可用 |
| `TOOL_TIMEOUT` / `TOOL_FAILED` | 工具超时/失败 | Agent 换路径或告知 |
| `MCP_SERVER_DOWN` | MCP server 不可用 | 自动重启 + 通知 |
| `SANDBOX_ERROR` / `SANDBOX_TIMEOUT` | 沙箱失败/超时 | 提示改写代码 |
| `UPLOAD_REJECTED` | 上传被拒（大小/类型/配额） | 展示具体原因 |
| `PARSE_FAILED` | 解析失败 | 记录 `parser_used`，可重试 |
| `NOT_FOUND` | 资源不存在 | — |
| `INTERNAL` | 内部错误 | 展示 trace_id 便于排查 |

---

## 附录 D：术语表

| 术语 | 含义 |
|------|------|
| **ADR** | Architecture Decision Record，架构决策记录 |
| **HITL** | Human-in-the-Loop，人工介入审批 |
| **RRF** | Reciprocal Rank Fusion，倒数排名融合（`score = Σ 1/(k+rank)`） |
| **RLS** | Row Level Security，PostgreSQL 行级安全策略 |
| **降级** | 依赖不可用时退化为次优路径，且必须对用户可见 |
| **噪声地板** | 同一代码重复评测时指标的固有波动范围（用 σ 度量） |
| **影子评测** | 用真实流量同时跑新旧管线并离线对比 |
| **fail-closed** | 不确定时默认拒绝（安全优先） |

---

**文档状态**：v1.1 待评审 → 评审通过后进入 W1 开发。
**变更记录**：v1.0（2026-09-22 初稿）→ v1.1（2026-09-22 按面试官视角评审修订，修正 2 处技术事实错误、补齐 6 类工程盲区、恢复 v1 检索评测资产、排期 8→12 周）。



