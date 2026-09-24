# 架构与关键时序

> 本文件是交付物"架构图 / 时序图"（设计文档 §20 P1）。
> 用 Mermaid 编写：GitHub 原生渲染、纯文本可 diff、不需要额外的图片工具链。

## 1. 组件图

```mermaid
graph TB
    subgraph client["浏览器"]
        WEB["Next.js 15 前端<br/>对话 / 知识库 / 检索调试台"]
    end

    subgraph edge["入口"]
        CADDY["Caddy<br/>反代 + SSE flush_interval -1"]
    end

    subgraph app["应用层"]
        API["FastAPI<br/>认证 · 会话锁 · 幂等 · SSE 桥接"]
        RUNTIME["Agent Runtime (LangGraph)<br/>planner → executor → ReAct"]
        TOOLS["ToolRegistry<br/>策略/风险/幂等/超时/截断/埋点"]
        RETRIEVAL["检索层<br/>向量 + 关键词 → RRF → 精排"]
    end

    subgraph mcp["MCP 工具（stdio 短会话）"]
        M1["sandbox<br/>RestrictedPython + rlimit"]
        M2["todo<br/>worker DB"]
        M3["search<br/>Tavily / DDG"]
        M4["email<br/>模拟发件（L2 审批）"]
    end

    subgraph data["数据与外部依赖"]
        PG[("PostgreSQL 16 + pgvector<br/>业务表 + 向量 + checkpoint")]
        REDIS[("Redis<br/>会话锁 · 幂等 · 事件流 · 决策缓存")]
        LF["Langfuse Cloud<br/>trace + span"]
        LLM["DeepSeek<br/>LLM"]
        EMB["DashScope<br/>Embedding + Rerank"]
    end

    subgraph async_["异步"]
        CELERY["Celery worker<br/>ingest 队列（同步 psycopg3）"]
    end

    WEB --> CADDY --> API
    API --> RUNTIME
    RUNTIME --> TOOLS --> mcp
    RUNTIME --> RETRIEVAL
    API --> PG
    API --> REDIS
    API -- 上传 --> CELERY
    CELERY --> PG
    RETRIEVAL --> PG
    RETRIEVAL --> EMB
    RUNTIME --> LLM
    API -. trace .-> LF
    RUNTIME -. trace .-> LF

    classDef ext fill:#eef,stroke:#88a
    class LLM,EMB,LF ext
```

## 2. 检索管线与降级分支（M6）

```mermaid
flowchart LR
    Q["query<br/>（jieba 分词供关键词路）"] --> V & K

    V["向量路<br/>cosine + user_id + iterative_scan"] -->|超时 500ms / 失败| DV["degraded:vector"]
    K["关键词路<br/>tsv + ts_rank_cd（独立连接）"] -->|超时 500ms / 失败| DK["degraded:keyword"]

    V & K --> RRF["RRF 融合 k=60<br/>候选 50"]
    RRF --> RR{"精排开关"}
    RR -->|开| BGE["DashScope rerank<br/>top 6"]
    RR -->|关 / 失败 / 超时 800ms| DR["degraded:rerank<br/>（UI：未精排）"]
    BGE --> ASSEMBLE["父块回溯去重<br/>注入上下文"]
    DR --> ASSEMBLE
    DV & DK --> RRF

    ASSEMBLE --> ANS["生成 + groundedness 校验"]
    ANS -->|无出处事实句 > 20%| RW["重写一次"]

    classDef deg fill:#fee,stroke:#c66
    class DV,DK,DR deg
```

## 3. SSE 两步流时序（ADR-10）

```mermaid
sequenceDiagram
    autonumber
    participant U as 浏览器
    participant A as FastAPI
    participant R as Redis Stream
    participant G as Agent 图

    U->>A: POST /chat（Idempotency-Key）
    A->>A: 取会话锁 → 幂等占位 → 落库 user 消息 + assistant(streaming)
    A-->>U: 200 {message_id, stream_url}
    A->>G: asyncio.create_task（后台推进，不绑定 HTTP）
    U->>A: GET /chat/{id}/stream
    A->>R: XRANGE 补历史
    G->>R: XADD token / citations / degraded
    R-->>A: 实时事件
    A-->>U: SSE（每条带 id:）
    Note over U,A: 用户关页面 → 任务继续跑（F4）<br/>重连带 Last-Event-ID 续读
    G->>G: 生成完成 → 落库 assistant(completed)
    G->>R: XADD done
    R-->>U: done + 完整答案
```

## 4. HITL 审批与进程崩溃恢复时序（M5 的 F1/F3）

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent 图
    participant P as PolicyRegistry
    participant DB as approvals 表
    participant H as 人工
    participant CK as Postgres checkpoint

    A->>P: invoke(send_email, to=[外部域])
    P->>P: 参数级升级 → L2
    P->>DB: INSERT approvals(pending)
    P-->>A: ApprovalInterrupt
    A->>A: interrupt(payload)
    A->>CK: 保存 checkpoint（图暂停）
    Note over A,CK: 消息置 interrupted · 会话锁释放<br/>**进程可以死**
    H->>DB: POST /approvals/{id}/decide {approved:true}
    DB->>DB: pending → approved
    H->>A: Command(resume=决策)（**新进程也可以**）
    A->>CK: 读取 checkpoint 续跑
    A->>P: 带 approved_approval_id 重新 invoke
    P->>DB: 校验 approved → 执行 → 标记 executed
    A-->>H: 续跑完成（事件继续写同一条流）
```

## 5. 多租户数据隔离（ADR-9）

```mermaid
flowchart TB
    REQ["请求（Bearer JWT）"] --> AUTH["认证 → UserCtx(user_id)"]
    AUTH --> LOCK["会话锁 Redis SET NX"]
    LOCK --> TX["tenant_session(user_id)<br/>事务内 set_config('app.user_id', uid, true)"]
    TX --> RLS["PostgreSQL RLS 策略<br/>current_setting('app.user_id') = user_id"]
    TX --> REPO["Repository 显式 WHERE user_id<br/>（双保险）"]

    RLS --> T1[("chunks / documents")]
    RLS --> T2[("conversations / messages")]
    RLS --> T3[("approvals / todos / 埋点")]
    T4[("checkpoint 表")] -.->|app_worker BYPASSRLS| W["Celery / checkpointer"]
    T5[("迁移")] -.->|app_owner| M["alembic"]

    classDef sec fill:#efe,stroke:#696
    class RLS,REPO sec
```

**读法**：普通租户流量走 `app_api`（受 RLS 约束，且 SQL 里再显式带 `user_id`）；
后台跨租户任务走 `app_worker`（BYPASSRLS）；DDL 走 `app_owner`。跨租户访问返回 **404 而非 403**——不泄露资源是否存在。

## 6. 里程碑与验收（速览）

```mermaid
timeline
    title 里程碑推进（每个里程碑都有独立验收手册）
    M0 最小闭环 : SSE 逐字回流（A1–A6）
    M1 基础设施与多租户 : JWT 轮转 · RLS · 会话锁 · 幂等（B1–B8）
    M2 知识库入库 : 上传校验 · 父子分块 · 千问向量化（C1–C7）
    M3 单路 RAG + 观测 : pgvector 检索 · 引用脚注 · Langfuse（D1–D7）
    M4 Agent 运行时 : planner DAG · 并行执行 · MCP 治理（E1–E8）
    M5 HITL 与流式健壮性 : 审批闭环 · kill -9 恢复 · 取消/重生成（F1–F6）
    M6 生产级 RAG : 混合检索 · RRF · 精排 · 降级矩阵（G1–G4）
    M7 评测与 CI : 真实语料评测集 · 对比归因 · GitHub Actions（H1–H3）
```
