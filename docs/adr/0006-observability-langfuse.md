# ADR-6：可观测性选 Langfuse（Cloud 版）+ PII 脱敏强制

> 状态：**已实现**（M3 落地 trace 与双钩子脱敏）
> 实现落点：`agent/obs/langfuse.py`（trace/span 封装 + 两个 mask 钩子）、`agent/obs/redact.py`（正则脱敏）
> 依赖版本：`langfuse 4.15.4`（Cloud，`us.cloud.langfuse.com`）

## 背景

LLM 应用的排障依赖四件事：**逐次调用的 trace/span**、**token 与成本计量**、**用户反馈**、**prompt 版本**。自建这四件事没有差异化价值，而 SDK 默认行为有一个**隐私陷阱**：会把 input/output 全量上报。

## 决策

用 **Langfuse Cloud 免费层**承载 LLM 专项观测；每次 chat 一条 trace（root span `chat`，子 span `retrieval` / `llm` / `llm_rewrite`），采样 100%（量小）。**强制 PII 脱敏**，且**自动化断言**。

## 替代方案

| 方案 | 放弃理由 |
|------|---------|
| Langfuse 自托管 | v3 起自托管需要 ClickHouse + Redis + S3/MinIO + 自有 PG **四个额外有状态服务**，个人项目运维成本与收益不成比例 |
| Phoenix (Arize) | 面板能力较弱，成本计量与 prompt 管理不足 |
| 纯 OpenTelemetry + 自建面板 | 要自己实现 LLM 语义约定、成本计算、反馈收集，工作量大且无差异化 |

## 代价与局限（含实测踩坑，这是本 ADR 的核心价值）

### 1. trace 通道的脱敏要用**两个钩子**，只做一个会漏

| 钩子 | 覆盖范围 | 签名（写错会静默失效） |
|------|---------|----------------------|
| `mask=` | SDK API 写入路径（`start_observation` / `update`） | **`(*, data)`** —— 初版写成位置参数，SDK 抛 TypeError 后**退回内置 fallback**，脱敏看似工作实则未执行（M3 验收 D4 实锤） |
| `mask_otel_spans=` | **导出路径**（含第三方自动插桩产生的 span） | `(*, params)`，且返回**必须是 `MaskOtelSpansResult` 实例** —— 返回普通 dict 会被判非法并**丢弃整批 trace**（比脱敏失败更严重） |

### 2. 上游 API 的迁移：旧接口对新组织已停用

Langfuse Cloud 对 2026-09 之后创建的组织**停用了 legacy trace API**（返回 410），读数据要用 `GET /api/public/v2/observations?fromStartTime=…&toStartTime=…`。文档里没写这一条时，排障会误以为是自己的调用写错了。

### 3. 已知未闭环项：trace 级 `userId`

OTel attribute（`user.id` / `session.id`）在 span 上**已正确写入**（官方 `propagate_attributes` 与直接 `set_attribute` 都验证过），但 v2 observations API 读回为空。当前 `user_id` / `conversation_id` 通过 span metadata 记录（可按 metadata 检索）。**这是一个没有闭环的点，如实记录而非含糊带过。**

### 4. 与 pgvector 侧的可观测缺口

设计文档 §14.1 提到要监控"召回阶段过滤后有效率"来触发 HNSW 分区迁移——**该指标未实现**（当前规模未到需要分区的程度，但指标缺口是真实的）。

## 影响

- PII 脱敏成为**默认行为**（不是可选项）：25 条单测覆盖手机号 / 身份证 / 邮箱 / `sk-` 密钥 / Bearer / JWT，其中"中英贴身"场景（`密钥sk-xxx`）暴露过 `\b` 与 CJK 的边界陷阱（Python 的 `\b` 把 CJK 当 word 字符）。
- D4 验收（Langfuse 里搜不到测试手机号）与这套断言互为印证：**验收发现问题 → 修实现 → 补断言**，是这条链路的完整闭环。
