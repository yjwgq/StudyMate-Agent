# Personal Agent OS — 个人 AI 助理平台改造设计文档

> ⚠️ **本文档已被取代，请勿依据本文档开发。**
> 现行设计依据：`PersonalAgent_设计文档_v1.1.md`（同目录）；施工依据：项目根目录 `项目实施计划.md`。
> 本文档保留仅用于追溯 v1.0 的原始设计与评审过程（见 `PersonalAgent_设计文档_评审报告.md`）。

> 版本：v1.0（施工蓝图）
> 日期：2026-09-22
> 状态：**已废弃（Superseded by v1.1）**
> 前身：StudyMate Agent（LangGraph 多 Agent 学习助手）
> 定位：面向个人用户的多用户 AI 助理平台，支持长期记忆、工具调用（MCP）、生产级 RAG、主动服务与全链路可观测。

---

## 0. 阅读指引

本文档是后续 6-8 周开发的唯一权威蓝图，包含：
- 架构决策（ADR）与技术选型理由（面试可直接引用）
- 数据库 DDL、Agent 状态机、API 契约（可直接开工）
- 每周验收标准（Definition of Done）
- 评测与量化指标方案（简历数据来源）

约定：
- 所有新代码在新仓库结构中开发，旧 StudyMate Agent 资产（沙箱、4 个 Agent、文档管线、双模型工厂）按附录 A 迁移映射搬运。
- 语言：Python 3.12（后端/Worker/Agent）+ TypeScript（前端）。
- 一切接口设计优先异步（async/await）。

---

## 1. 项目背景与目标

### 1.1 背景

原 StudyMate Agent 是一个"路由→执行→反思"固定 DAG 的学习问答 demo，具备：LangGraph 编排、Chroma 单路向量 RAG、RestrictedPython 计算沙箱、本地/云端双模型、FastAPI+Streamlit。

其工程短板：固定工作流非真正 Agent、单路 RAG、无记忆、单用户无认证、无流式、单机文件存储、无可观测性、无评测、无容器化。

### 1.2 目标（北极星）

打造一个可作为一线 AI 应用开发岗代表作的**个人 AI 助理平台**，具备企业级产品的关键工程特征：

1. **Agentic**：任务规划 + ReAct 工具循环 + 中断恢复 + 人工审批（HITL），MCP 工具生态。
2. **生产级 RAG**：混合检索 + RRF + Rerank + 查询改写，答案带引用且可量化评测。
3. **长期记忆**：三级记忆体系 + 定时巩固 + 个性化。
4. **主动服务**：定时简报、提醒（Agent 主动触达而非被动问答）。
5. **企业工程化**：多租户、SSE 流式、限流缓存、护栏、Langfuse 追踪、RAGAS 评测门禁、Docker/CI。

### 1.3 非目标（MVP 不做，防止 scope 失控）

- 移动端 App、语音输入/合成、多模态图片生成
- GraphRAG / 知识图谱（列为 v2 候选）
- 插件市场、第三方用户分享协作
- 微服务拆分、Kubernetes、Kafka/RocketMQ（个人项目过度设计）
- Java/Spring 混合架构（纯 Python 保证一人可控、叙事连贯）

### 1.4 角色与使用场景

| 场景 | 示例 |
|------|------|
| 知识问答 | "我上周剪藏的那篇关于 RAG 评测的文章说了什么？"（跨个人库，带引用） |
| 事务代办 | "明天下午 3 点提醒我交周报" → 调用 todo/calendar 工具，需人工确认 |
| 深度研究 | "调研 2026 年主流向量数据库对比，输出带引用报告"（多轮检索+交叉验证） |
| 主动简报 | 每天 08:30 推送：天气、日程、待办、关注领域动态、未读摘要 |
| 学习解题 | 保留：数理题沙箱验算 + 五步讲解（作为一个 skill 子图） |

---

## 2. 总体架构

### 2.1 架构图

```
┌──────────────────────────────────────────────────────────────────┐
│ 前端  Next.js 15 (App Router) · TS · shadcn/ui · Vercel AI SDK    │
│   聊天(SSE流式) · 知识库管理 · 记忆/画像 · 简报 · 审批中心          │
└───────────────┬──────────────────────────────────┬───────────────┘
                │ HTTPS / SSE                       │ REST(JSON)
┌───────────────▼──────────────────────────────────▼───────────────┐
│ FastAPI (async)  接入层                                            │
│  JWT/OAuth2 认证 · 多租户中间件 · 限流(slowapi) · 请求校验         │
│  Chat(SSE) / KB / Memory / Briefing / Approval / Admin            │
└───────┬───────────────────────────┬──────────────────┬───────────┘
        │                           │                  │
┌───────▼──────────────┐  ┌─────────▼─────────┐  ┌────▼────────────┐
│  Agent Runtime       │  │  Retrieval Service │  │ Memory Service  │
│  LangGraph           │  │  hybrid+rerank     │  │ 三级记忆         │
│  Planner→ReAct→HITL  │  │  query rewrite     │  │ 巩固/画像提取     │
│  Checkpointer(PG)    │  │  citation/ground   │  └────┬────────────┘
│  Guardrails(总线)    │  └─────────┬─────────┘       │
└───────┬──────────────┘            │                 │
        │ MCP (stdio/HTTP)         │                 │
┌───────▼──────────────────────────▼─────────────────▼────────────┐
│ MCP Servers: todo · calendar · web-search · mail · filesystem    │
│              · sandbox(复用RestrictedPython) · fetch              │
└──────────────────────────────────────────────────────────────────┘
        │ 定时触发(beat)            ▼ 主动推送
┌───────▼──────────────────────────────────────────────────────────┐
│ Celery Worker + Beat：每日简报 · 提醒 · 记忆巩固 · 文档入库        │
└──────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│ 基础设施  PostgreSQL16+pgvector · Redis · Langfuse · Docker Compose│
│           GitHub Actions(lint/test/eval门禁) · RAGAS 评测         │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 请求生命周期（一次带工具调用的问答）

```
前端(POST /api/chat, SSE)
 → 鉴权/限流/租户上下文
 → Langfuse trace 开始
 → 注入护栏：输入注入检测 + PII 脱敏
 → 记忆检索（用户画像/相关历史）动态注入 system prompt
 → Planner 产出 Plan（任务列表）
 → ReAct 循环：思考 → 选择工具(MCP) → 执行(危险动作入审批) → 观察
       · 知识类任务 → Retrieval Service（改写→混合召回→RRF→rerank→拼上下文）
 → Synthesizer 生成答案（强制引用脚注）
 → 护栏：groundedness 校验 + 输出审核
 → 写情景记忆；异步投递语义记忆巩固任务
 → SSE 逐 token/逐事件回流前端；trace 落 Langfuse（token/成本）
```

---

## 3. 架构决策记录（ADR）

### ADR-1：Agent 框架选 LangGraph（而非 AutoGen/CrewAI/裸 LangChain）
- 现有团队已熟悉 LangGraph；其显式状态机、Checkpointer、子图、interrupt 机制是"中断恢复/HITL"刚需。
- CrewAI 偏角色模板、可控性弱；AutoGen 对话式编排难持久化。
- 代价：图定义较啰嗦——通过节点基类与装饰器封装缓解。

### ADR-2：存储统一 PostgreSQL + pgvector（而非 Chroma/Milvus/ES 多套）
- 个人项目规模（百万级 chunk 内）pgvector 的 HNSW 完全够用；业务数据与向量同库，支持事务一致性与 JOIN 过滤。
- 关键词检索用 PG 全文检索（`tsvector`，中文用 zhparser 或简化的 pg_trgm）起步，避免引入 ES 运维负担。
- 迁移成本：旧 Chroma 数据仅 31 条样例，直接废弃重建。

### ADR-3：前端 Next.js 15 + Vercel AI SDK（而非保留 Streamlit/Vue）
- 流式对话、工具调用过程渲染、审批交互在 React 生态有最佳库支持（AI SDK `useChat` 原生支持 tool-call 状态与 SSE）。
- shadcn/ui 提供企业级观感。Streamlit 保留为内部调试台（可选）。

### ADR-4：工具体系采用 MCP（Model Context Protocol）
- MCP 是工具/资源/提示词的标准协议（stdio+HTTP 传输），一次封装多端复用，2025-2026 行业热点，简历差异化强。
- 至少 1 个自建 MCP Server（sandbox），其余接官方/第三方（filesystem、fetch、brave/tavily-search）。
- 兜底：对不适合 MCP 的薄封装（如内部 todo CRUD）允许直接 Python 函数工具，但必须实现统一 ToolSpec 接口。

### ADR-5：异步任务用 Celery + Redis（而非纯 APScheduler / 后台线程）
- 简报、提醒、记忆巩固、文档入库都是可重试、需持久化、需定时（beat）的任务；Celery 是 Python 事实标准且面试认知度高。
- FastAPI 内的轻量异步用 `asyncio`/`arq` 兜底，不为此再引组件。

### ADR-6：可观测性选 Langfuse（开源自建）
- 原生支持 LangChain/LangGraph 回调、trace/span、token&成本、用户反馈打分、prompt 版本管理；Docker 一键起。
- OpenTelemetry 作为日志/指标标准输出，Langfuse 作为 LLM 专项观测，二者互补不冲突。

### ADR-7：评测用 RAGAS + 自建 golden set，纳入 CI 门禁
- 指标：faithfulness（忠实度）、answer_relevancy、context_precision/recall。
- 200 条人工标注集，W2 建立基线，W4/W7 回归对比。CI 中分数回退超过阈值则红灯。

### ADR-8：模型双模式保留（云端 API + 本地 Ollama）
- 通过统一 `LLMProvider` 抽象 + 配置切换；默认云端（DeepSeek/Qwen3）保障效果，离线可切 Ollama。
- Embedding/Rerank 同理（云端用 DashScope，本地用 bge 系列 + Ollama/Infinity）。

### ADR-9：多租户隔离采用"共享库 + 行级 tenant_id"（而非 schema-per-user）
- 个人助理租户=用户，数量级有限；所有业务表强制 `user_id` 外键，Repository 层 + DB 行级安全策略（RLS）双保险。

---

## 4. 技术栈定稿

| 层 | 选型 | 版本约束（建议） |
|----|------|----------------|
| 语言 | Python | 3.12 |
| Web | FastAPI、Uvicorn、slowapi、pydantic v2 | fastapi>=0.115 |
| Agent | langgraph、langchain、mcp（Python SDK） | langgraph>=0.3（checkpoint-postgres） |
| 模型 | openai SDK（兼容 DeepSeek/通义/Ollama） | - |
| 异步任务 | celery[redis]、redis-py | celery>=5.4 |
| 数据库 | PostgreSQL 16、pgvector、SQLAlchemy 2（async）、Alembic | - |
| 检索 | pgvector HNSW、tsvector/pg_trgm、bge-reranker（infinity/tei 或 API） | - |
| 鉴权 | python-jose、passlib[bcrypt]、OAuth2 Password Flow | - |
| 观测 | langfuse、structlog、OpenTelemetry | - |
| 评测 | ragas、datasets、pytest | - |
| 前端 | Next.js 15、React 19、TypeScript 5、Tailwind、shadcn/ui、Vercel AI SDK 5 | - |
| 部署 | Docker、Docker Compose、GitHub Actions | - |
| 质量 | ruff、mypy、pytest、pre-commit | - |

---

## 5. 代码结构

```
personal-agent/
├── apps/
│   ├── api/                     # FastAPI 接入层
│   │   ├── app.py
│   │   ├── core/                # config/security/deps/tenant/ratelimit
│   │   ├── api/v1/              # auth chat kb memory briefing approval admin
│   │   └── sse.py               # SSE 事件协议
│   └── web/                     # Next.js 15
│       ├── app/                 # App Router: chat / kb / memory / briefing / login
│       ├── components/
│       └── lib/api.ts
├── agent/
│   ├── provider/                # LLMProvider 双模型工厂（迁自 base_agent）
│   ├── graph/
│   │   ├── builder.py           # 图装配
│   │   ├── state.py             # AgentState/Plan/Step
│   │   ├── nodes/               # planner react synthesizer reflection(子图) study(子图)
│   │   └── routing.py
│   ├── tools/
│   │   ├── base.py              # ToolSpec 统一接口
│   │   ├── builtin/             # todo/search/mail/...
│   │   └── mcp_client.py        # MCP 连接池/工具发现
│   ├── memory/
│   │   ├── buffer.py episodic.py semantic.py consolidation.py
│   ├── retrieval/
│   │   ├── ingest.py splitter.py parser(迁data_process)
│   │   ├── hybrid.py rerank.py rewrite.py citations.py
│   ├── guardrails/
│   │   ├── injection.py pii.py moderation.py groundedness.py
│   └── callbacks/               # langfuse/sse 回调
├── mcp_servers/
│   ├── sandbox_server/          # 自建：RestrictedPython 沙箱（迁 tools/code_sandbox）
│   └── todo_server/             # 自建：待办（演示协议定义能力）
├── worker/
│   ├── celery_app.py tasks/     # briefing reminder consolidation embed
├── infra/
│   ├── docker-compose.yml       # postgres redis langfuse api worker beat infinity
│   ├── docker/                  # Dockerfile.api / Dockerfile.worker / Dockerfile.web
│   └── alembic/                 # 迁移脚本
├── evals/
│   ├── golden_200.jsonl ragas_run.py ci_eval.py
├── scripts/
├── tests/                       # 单测/集成（pytest）
└── .github/workflows/ci.yml
```

---

## 6. 数据模型设计（PostgreSQL DDL 要点）

所有表含 `created_at/updated_at`；业务表带 `user_id` 并启用 RLS。

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 用户与认证
CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  display_name  TEXT,
  settings      JSONB NOT NULL DEFAULT '{}',   -- 简报时间/关注主题/模型偏好
  created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE refresh_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, revoked BOOL DEFAULT FALSE
);

-- 会话与消息
CREATE TABLE conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  title TEXT, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT CHECK (role IN ('user','assistant','tool','system')),
  content TEXT,
  tool_calls JSONB,                  -- 工具调用轨迹
  token_usage JSONB,                 -- prompt/completion/total
  trace_id TEXT,                     -- Langfuse trace
  feedback SMALLINT,                 -- 用户点赞点踩
  created_at TIMESTAMPTZ DEFAULT now()
);

-- 知识库（文档/分块/向量）
CREATE TABLE documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  title TEXT, source TEXT, source_type TEXT,   -- upload/webclip/note
  status TEXT DEFAULT 'processing',            -- processing/ready/failed
  meta JSONB DEFAULT '{}', created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
  user_id UUID NOT NULL,
  parent_id UUID,                    -- 父子分块：精排用子块，喂给LLM用父块
  content TEXT NOT NULL,
  tsv tsvector,                      -- 关键词检索
  embedding vector(1024),            -- 维度随嵌入模型，建库时锁定
  meta JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX chunks_vec_idx ON chunks
  USING hnsw (embedding vector_cosine_factor_ops) WITH (m=16, ef_construction=128);
CREATE INDEX chunks_tsv_idx ON chunks USING gin(tsv);
CREATE INDEX chunks_user_idx ON chunks(user_id);

-- 三级记忆
CREATE TABLE episodic_memories (      -- 情景：被提炼过的对话片段
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  conversation_id UUID, summary TEXT NOT NULL,
  embedding vector(1024), importance REAL DEFAULT 0.5, happened_at TIMESTAMPTZ
);
CREATE TABLE semantic_memories (      -- 语义：用户画像/偏好/事实
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT CHECK (kind IN ('profile','preference','fact','instruction')),
  key TEXT, value TEXT, embedding vector(1024),
  confidence REAL DEFAULT 0.8, hit_count INT DEFAULT 0,
  last_used_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, kind, key)
);

-- 待办/提醒
CREATE TABLE todos (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL, detail TEXT, due_at TIMESTAMPTZ,
  status TEXT DEFAULT 'pending',       -- pending/done/cancelled
  source TEXT DEFAULT 'chat'
);

-- Agent 检查点（LangGraph 官方表结构由 langgraph-checkpoint-postgres 迁移生成）
-- 略：checkpoints / checkpoint_writes / checkpoint_blobs

-- 审批（HITL）
CREATE TABLE approvals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  thread_id TEXT NOT NULL,            -- LangGraph thread
  action JSONB NOT NULL,              -- 待执行的工具调用
  status TEXT DEFAULT 'pending',      -- pending/approved/rejected/expired
  decided_at TIMESTAMPTZ
);

-- 简报
CREATE TABLE briefings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  content_md TEXT, sections JSONB,
  delivered_at TIMESTAMPTZ DEFAULT now()
);
```

**分块策略**：父子分块（small-to-child / large-to-parent）——子块 256 token 用于精准召回，命中后回溯父块 1024 token 喂模型；重叠 10%。

**维度一致性约定**：`chunks.embedding` 维度在 `.env` 锁定（云端 1024 / 本地 bge-m3 1024）；切换嵌入模型必须跑迁移脚本（重嵌入），由启动时维度自检拦截。

---

## 7. Agent 运行时设计（核心）

### 7.1 顶层图（Plan-and-Execute + ReAct）

```
START
  → guard_input（注入/PII）
  → retrieve_memory（画像+相关情景，注入 state.system_context）
  → planner
       ├─ plan 为空/纯闲聊 ───────────────→ synthesize
       ├─ 含学习解题任务 ────────────────→ study_subgraph（保留五步+沙箱）
       └─ 一般任务 ─→ execute_steps（循环）
                          step → react_agent（ReAct 子图，多轮工具）
                                   ├─ 需要危险工具 → interrupt(HITL审批)
                                   ├─ 知识类问题 → retrieval 工具节点
                                   └─ 完成/失败 → step_router（下一step / replan）
                          （执行偏差大时可回 planner 重规划，最多 1 次）
  → synthesize（汇总各 step 结果，生成带引用答案）
  → guard_output（groundedness/审核）
  → write_memory → END
```

### 7.2 State 定义（Pydantic）

```python
class Step(BaseModel):
    id: int
    description: str
    tool_hint: str | None = None
    status: Literal["pending","running","done","failed","blocked"] = "pending"
    result: str = ""

class AgentState(BaseModel):
    messages: Annotated[list, add_messages]
    user_id: str
    plan: list[Step] = []
    current_step: int = 0
    replans_left: int = 1
    citations: list[Citation] = []
    system_context: str = ""          # 记忆注入
    retrieved: list[Chunk] = []
    require_approval: bool = False
    trace_id: str = ""
```

### 7.3 ReAct 子图与工具调用

- 每轮：LLM 决策（function calling）→ 若命中工具则执行 → Observation 回灌，最多 N=8 轮，强制终止条件：无新工具调用/超 N 轮/预算（token）超限。
- 工具分级：
  - **L0 只读**（search/retrieve/math）：自动执行
  - **L1 写操作**（todo 创建/mail 草稿保存）：执行但事后告知
  - **L2 不可逆/外发**（发送邮件、删除文件、日程邀请他人）：**interrupt → approvals 表 → 前端审批中心**，通过后 Checkpointer 恢复。
- Checkpointer：`PostgresSaver`，`thread_id = conversation_id`，支持长任务跨进程恢复与时间旅行调试。

### 7.4 工具协议（ToolSpec）

```python
class ToolSpec(BaseModel):
    name: str
    description: str
    args_schema: type[BaseModel]
    risk_level: Literal[0,1,2]
    async def arun(self, **kwargs) -> ToolResult: ...
```
MCP 工具在启动时由 `mcp_client` 发现并自动适配为 ToolSpec（含 risk_level 元数据约定）。

### 7.5 反思子图（保留并升级现有 ReflectionAgent）
- 事实校验：答案中的数值/专名必须能在 `retrieved` 或工具结果中找到出处，否则标记 ⚠ 或触发一次补充检索。
- 计算复核：数学表达式抽取后走 sandbox MCP 重算。
- 引用校验：每个 `[n]` 脚注必须能映射到真实 chunk，且引用片段与论断语义相关（reranker 分数阈值）。

---

## 8. RAG 管线设计

### 8.1 入库（Ingest，Celery 异步）
```
文件/剪藏 → parser(pdf/docx/md/html) → 父子分块
 → 关键词索引（中文分词→tsv）
 → embedding（批处理，失败重试，幂等 chunk_id = hash(doc_id+idx+content)）
 → 写 chunks（同事务写业务状态 documents.status=ready）
```

### 8.2 检索（在线，预算 < 800ms P95）
```
query
 → query_rewrite：① HyDE 生成假设答案 ② 子问题拆分（复杂问题 ≤3）
 → 并行召回（各 50）：
     · 向量路：embedding cosine（<=>）+ user_id 过滤 HNSW
     · 关键词路：tsv 全文 / trigram 模糊
 → RRF 融合（k=60）去重 → 候选 ~30
 → Reranker（bge-reranker-v2-m3，cross-encoder）精排 top 6
 → 父块回溯 → 注入上下文（附 chunk_id 用于引用）
 → Redis 缓存（query+user 维度，TTL 10min，命中率目标 >40%）
```

### 8.3 引用与 groundedness
- 答案中以上标 `[1][2]` 标注，前端悬浮显示来源文档标题/段落，可跳转。
- groundedness：NLI 风格逐句核验，无出处句子比例 >20% 则触发一次"针对性补充检索 + 重写"，仍不达标则在 UI 明确标黄"该结论缺少资料支撑"。

---

## 9. 记忆系统设计

| 层级 | 存储 | 写入时机 | 检索方式 | TTL 策略 |
|------|------|---------|---------|---------|
| 短期 Buffer | Redis | 当轮会话 | 直接拼接最近 K=10 轮 | 会话结束转情景 |
| 情景 Episodic | PG 表 + 向量 | Worker 定时巩固：会话结束 30min 后摘要成条目 | 按 query 向量召回 top 5 | 重要度衰减，>90 天低分归档 |
| 语义 Semantic | PG 表 + 向量 | Worker 从情景中 LLM 抽取（profile/preference/fact/instruction） | 画像全量注入 + key 精确匹配 | 命中强化、冲突时新值覆盖旧值（保留审计） |

巩固任务（Celery beat，每小时）：
1. 扫描未巩固会话 → LLM 生成情景摘要 + 重要度打分；
2. 从新情景抽取语义记忆，与现有条目做冲突消解（如用户改口"我不用 Java 了"→ 覆盖）；
3. 前端"记忆"页可查看/编辑/删除（用户可控是信任关键）。

---

## 10. 主动服务（Celery Worker）

- **每日简报** beat（按用户 settings 时区/时间）：聚合天气、当日日程、到期 todo、关注主题的 web-search 结果、知识库更新 → LLM 组织成 sections JSON → `briefings` 表 → 前端站内通知 + 可选 webhook（Server酱/飞书机器人）。
- **提醒**：todo.due_at 到期前 10min 推送。
- **入库/巩固**：见 §8.1、§9。
- 重试策略：指数退避 3 次，死信表 + 管理后台可见。

---

## 11. API 契约（v1，摘要）

统一响应：成功 `{data, trace_id}`，失败 `{error:{code,message,detail}}`；SSE 通道单列。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/auth/register` `/login` `/refresh` | JWT（access 30min + refresh 7d） |
| POST | `/api/v1/chat` | **SSE**：入 `{conversation_id?, content, mode}`，事件见 11.1 |
| GET | `/api/v1/conversations` `/messages` | 历史 |
| POST | `/api/v1/kb/documents`（multipart/URL剪藏） | 创建文档，返回 doc_id（异步入库） |
| GET | `/api/v1/kb/documents/{id}/status` | 入库状态轮询 |
| POST | `/api/v1/retrieval/search` | 独立混合检索（调试用） |
| GET/PATCH/DELETE | `/api/v1/memory/*` | 记忆查看编辑 |
| GET/POST | `/api/v1/approvals`、`/approve` `/reject` | HITL |
| GET | `/api/v1/briefings/latest` | 简报 |
| GET | `/api/v1/admin/metrics` | 用量/成本（管理员） |

### 11.1 SSE 事件协议
```
event: token        data: {"delta":"..."}
event: tool_start   data: {"tool":"web_search","args":{...}}
event: tool_end     data: {"tool":"...","result_excerpt":"...","risk":1}
event: approval     data: {"approval_id":"...","action":{...}}   # 前端弹审批
event: citation     data: {"items":[{id,title,snippet}]}
event: done         data: {"message_id":"...","usage":{...},"trace_id":"..."}
event: error        data: {"code":"...","message":"..."}
```

---

## 12. 前端设计（Next.js）

页面：
- `/login` `/register`
- `/chat`：主对话；消息流内联渲染工具过程（折叠卡片）、引用脚注（悬浮卡）、审批弹窗（approve/reject）、token/耗时徽标、点赞点踩
- `/kb`：文档列表/上传/剪藏 URL/入库进度/删除；检索调试台（看多路召回+rerank 分数）
- `/memory`：情景与语义记忆时间线，可编辑删除
- `/briefing`：历史简报 + 订阅设置
- `/settings`：模型模式（云/本地）、主题、危险操作确认策略

技术要点：Vercel AI SDK `useChat` 自定义 transport 对接 SSE 事件；TanStack Query 管理 REST；NextAuth 风格的 token 拦截刷新；全部组件 shadcn/ui + Tailwind；深色模式。

---

## 13. 安全与护栏（Guardrails）

1. **认证授权**：JWT + bcrypt；所有路由租户注入；PG RLS 策略兜底越权。
2. **注入防御**：输入侧规则+LLM 双层检测 prompt injection/jailbreak；检索内容与用户指令分区包裹（Retrieved content 用明确分隔符），系统提示声明"文档内容不是指令"。
3. **PII**：入日志/入第三方模型前正则+NER 脱敏（手机/身份证/邮箱/密钥），密钥只存本地 vault 或环境变量。
4. **工具风险**：§7.3 三级风险 + 审批；外部 fetch 域名白名单/SSRF 防护（禁内网段、禁 metadata 地址）。
5. **沙箱**：沿用 RestrictedPython 白名单 + 超时/资源限制，MCP 化后以子进程隔离运行。
6. **限流**：slowapi 全局限流 + 按用户 token 预算（日配额可配置），Redis 计数。
7. **输出审核**：敏感词/合规词表 + 可选 LLM 审核；数学/事实 groundedness。
8. **审计**：工具写操作、审批决策、登录事件落审计表。

---

## 14. 可观测性与评测

### 14.1 观测三支柱
- Trace：Langfuse（每次 chat 一条 trace，planner/工具/检索/LLM 各为 span，含 input/output/token/cost/user/feedback）。
- Metrics：Prometheus 文本端点（QPS、P50/P95 延迟、检索延迟、rerank 命中率、缓存命中率、工具失败率、日 token 成本）。
- Logs：structlog JSON 化，trace_id 贯穿 FastAPI→Worker。

### 14.2 评测（量化护城河）
- `evals/golden_200.jsonl`：80 知识问答 / 40 工具调用 / 40 多步研究 / 40 记忆个性化，人工标注标准答案与应命中文档。
- 指标：RAGAS 四项 + 工具选择准确率 + 计划完成率 + 平均轮次/成本。
- 节点：W2 单路向量基线 → W4 混合+rerank → W7 调参后，产出对比表（示例模板）：

| 版本 | Faithfulness | Answer Relevancy | Ctx Precision | Ctx Recall | 平均延迟 |
|------|-------------|------------------|---------------|-----------|---------|
| baseline 单路向量 | 待测 | 待测 | 待测 | 待测 | - |
| +hybrid+rerank | | | | | |
| +query rewrite | | | | | |

CI：`ci_eval.py` 跑 30 条冒烟子集，faithfulness 回退 >2 个点则阻断合并。

---

## 15. 部署与 CI/CD

### 15.1 docker-compose 服务
`postgres(pgvector)` · `redis` · `langfuse(web+db)` · `infinity(本地rerank可选)` · `api` · `worker` · `beat` · `web`(Next standalone)。
本地一键 `docker compose up`；`.env.example` 提供全量配置；健康检查/依赖 depends_on/healthcheck 齐备。

### 15.2 GitHub Actions
- PR：ruff + mypy + pytest + 前端 build + 30 条 eval 冒烟。
- main：构建镜像、打 tag、（可选）部署到单机 VPS（docker compose pull + migrate）。
- Alembic 迁移在容器启动时自动 `upgrade head`。

---

## 16. 8 周计划与验收标准（DoD）

| 周 | 交付物 | 验收标准（可演示/可测） |
|----|--------|----------------------|
| W1 地基 | 仓库骨架；compose 起 pg/redis；FastAPI 异步+JWT+多租户；Next.js 登录+基础聊天 SSE | 注册登录拿到 JWT；/chat 流式直连模型；越权访问他人数据返回 403；`docker compose up` 全绿 |
| W2 数据+单路RAG+观测 | Alembic 全量表；文档入库管线；pgvector 单路检索问答；Langfuse trace | 上传 PDF 后异步可检索问答；Langfuse 可见完整 trace/token；**golden 集建立并跑出 baseline 分数** |
| W3 Agent 深化 | Planner+ReAct 图；3 个 MCP 工具（search/todo/sandbox）；Checkpointer+审批 | "加待办并提醒我"自动调工具；发邮件类动作被拦截进审批中心；kill 进程后审批通过能续跑 |
| W4 生产级RAG | 混合检索+RRF+rerank+HyDE/子问题；引用标注 | RAGAS 四项较 baseline 显著提升（目标 faithfulness +10pt）；答案每论断有可点击引用 |
| W5 记忆 | 三级记忆+巩固 Worker+记忆页 | 隔天问"我之前说过的偏好"能答出；记忆页可增删改；冲突偏好被正确覆盖 |
| W6 主动+护栏 | 每日简报 beat/提醒；注入检测/PII/SSRFI/限流 | 到点收到简报；10 条注入红队样本被拦；外发动作无审批不可执行 |
| W7 评测+性能 | golden200 全量跑分；指标看板；缓存/并发调优 | 产出三版本对比表写入 README；P95 首 token <2s、检索 P95<800ms；缓存命中>40% |
| W8 收尾 | CI 门禁、生产 compose、压测报告、Demo 视频、架构 README、简历点 | 全新机器 30 分钟内部署完成；CI 全绿；3 分钟 demo 覆盖问答/工具/记忆/简报/审批 |

每周五：tag、更新 README 进度、录屏存档（防烂尾）。

---

## 17. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 范围膨胀 | 烂尾 | 严守 §1.3 非目标；新想法进 backlog，不插队 |
| MCP 生态不稳定 | W3 延期 | mcp_client 与 ToolSpec 双轨，随时可退化为内置函数工具 |
| 云端成本失控 | 烧钱 | 日 token 配额 + 预算中断 + 小模型路由（分类用 Haiku 级，生成用主力模型） |
| RAGAS 中文打分偏差 | 指标不可信 | 主模型用 Qwen 做 judge + 50 条人工校准，报人工/自动双口径 |
| Windows 本地开发坑 | 环境问题 | 全部服务容器化；Python 只在 venv/devcontainer 跑 |
| 嵌入模型维度锁定 | 换模型报错 | 启动自检 + Alembic 重嵌入脚本，文档显式声明 |
| 一人项目测试覆盖不足 | 质量塌方 | 核心路径（图/检索/护栏/租户隔离）必须有 pytest，CI 强制执行 |

---

## 18. 简历呈现预演（项目完成后的 bullet 草稿）

1. 设计实现基于 LangGraph 的 **Plan-and-Execute 多 Agent 运行时**：ReAct 工具循环 + Postgres Checkpointer 中断恢复 + 三级风险人工审批，基于 **MCP 协议**接入/自建 6+ 工具。
2. 构建生产级 RAG：父子分块 +（BM25/tsvector 与 pgvector）混合检索经 RRF 融合与 BGE Reranker 精排，HyDE/子问题查询改写，200 条 RAGAS 评测集驱动，忠实度由 X% 提升至 Y%。
3. 实现三级长期记忆（情景/语义/用户画像）与 Celery 定时巩固/主动简报，LLM 个性化注入。
4. FastAPI async + SSE + JWT 多租户（PG RLS）+ Redis 限流缓存；Langfuse 全链路追踪与 token 成本计量；注入/PII/SSRF/沙箱多层护栏。
5. Docker Compose 一键部署，GitHub Actions 实现 lint/test/**eval 回归门禁**；P95 首 token <2s。

（X/Y 等指标在 W7 用实测数据填写，禁止编造。）

---

## 附录 A：旧资产迁移映射

| 旧文件 | 去向 |
|--------|------|
| `tools/code_sandbox.py` | `mcp_servers/sandbox_server/`（MCP 化 + 子进程隔离） |
| `agents/router_agent.py` | `agent/graph/nodes/planner.py` 的意图信号来源（弱化为 planner 的一个前置分类调用） |
| `agents/qa_exercise_agent.py` | `agent/graph/nodes/study_subgraph.py` |
| `agents/retrieve_agent.py` | 拆解：检索走 `retrieval/`，文献格式化保留为工具 |
| `agents/reflection_agent.py` | `agent/graph/nodes/reflection.py`（见 §7.5） |
| `agents/base_agent.py` | `agent/provider/`（LLMProvider 双模型工厂） |
| `data_process/*` | `agent/retrieval/{parser,splitter,ingest}.py` + worker 入库任务 |
| `graph/workflow.py` | `agent/graph/builder.py` 重写（参考其超时包装/单例注册表） |
| `api/main.py` | `apps/api/` 拆分路由 |
| `frontend/*`(Streamlit) | Next.js 重写；Streamlit 可留作 `/debug` 内部台（可选） |
| 旧 chroma_db（31 条样例） | 废弃，不迁移 |

## 附录 B：环境变量规划（.env.example 摘要）
```
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://...
USE_CLOUD_MODEL=true
CLOUD_LLM_BASE_URL / CLOUD_API_KEY / CLOUD_LLM_MODEL
EMBED_MODEL=text-embedding / bge-m3 ; EMBED_DIM=1024
RERANK_MODEL=bge-reranker-v2-m3
JWT_SECRET / ACCESS_TTL / REFRESH_TTL
LANGFUSE_HOST / PUBLIC_KEY / SECRET_KEY
MCP_SANDBOX_CMD / MCP_FILESYSTEM_ROOT / SEARCH_API_KEY
DAILY_TOKEN_BUDGET=500000
```
