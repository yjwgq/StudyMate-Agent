# ADR-5：异步任务用 Celery + Redis，但必须处理异步阻抗

> 状态：**已实现**（M2 落地 ingest 队列，M5 起 worker 与 api 共用镜像）
> 实现落点：`worker/celery_app.py`、`worker/tasks/ingest.py`、`worker/db.py`
> 队列：`ingest`（文档入库，独立 worker）

## 背景

文档入库是"重且慢"的任务：解析 PDF、去噪、父子分块、jieba 分词、批量 embedding（网络往返）、写 chunk。放在请求链路里会让上传接口长时间挂起；放在后台就绕不开"异步栈（FastAPI/asyncpg/AsyncOpenAI）与同步 worker（Celery prefork）"的阻抗。

## 决策

用 **Celery + Redis** 承载入库任务，**独立 `ingest` 队列**，`worker_prefetch_multiplier=1`（长任务公平分发），`task_soft_time_limit=600 / time_limit=660`。

## 替代方案

| 方案 | 优势 | 劣势 | 结论 |
|------|------|------|------|
| **Celery** | 生态成熟、重试/死信成熟、面试认知度高 | 同步模型与 async 栈有阻抗 | **选它**（但适配方式见下） |
| taskiq | async 原生、风格与 FastAPI 一致 | 生态较新，观测/重试不如 Celery 完善 | v2 候选 |
| arq | async 原生、轻量 | 功能少、社区小 | 备选 |
| APScheduler / 后台线程 | 零额外组件 | 不可持久化、不可重试、多进程重复执行 | 仅用于进程内轻量定时 |

## 与设计文档的差异（**实现方式完全不同，这是本 ADR 最重要的修正**）

设计文档给的适配层方案是"worker 用 `--pool=gevent` + 进程内常驻事件循环 + `loop.run_until_complete()`"。

**实际做法：消除阻抗，而不是适配阻抗** —— 整个 ingest 链路走**同步驱动**：

| 环节 | 设计文档 | 实际实现 |
|------|---------|---------|
| Celery pool | `gevent` | **默认 prefork**（无需 gevent） |
| 事件循环 | 进程内常驻 + `run_until_complete` | **完全不引入 asyncio** |
| 数据库 | asyncpg 连接池复用 | **psycopg3 同步**（`worker/db.py`，连接串 `+asyncpg` → `+psycopg`） |
| Embedding 调用 | async httpx | **同步 `openai.OpenAI`**（`agent/provider/embedding.py` 的 `EmbeddingClient`） |

为什么这样更好：**阻抗来自"同一进程里既要 async 又要 sync"，那就不要让 worker 有 async**。入库是一条纯批处理链路（无并发请求、无 SSE），同步实现更简单、更易调试，还省掉了 gevent monkey-patch 与常驻循环的复杂度。代价是：如果将来要在一个 task 内做高并发 IO（比如并行下载多个 URL），同步模型会成为瓶颈——那时的正确做法是拆分任务而不是改回 async。

## 代价与局限

1. **`acks_late=True` 未显式开启**：当前依赖任务幂等性（`chunks` 的 `UNIQUE(document_id, chunk_type, ord)` + 父块 upsert + 子块按已入库 ord 断点续传），重跑安全。若开 `acks_late` 需重新评估重复执行窗口。
2. **worker 与 api 共用镜像**：省构建时间，但意味着 worker 进程里也装了 SSE/FastAPI 相关依赖（镜像体积代价）。
3. **单队列**：M9 的简报/提醒任务落地时需要按队列拆分 worker，否则互相阻塞。

## 影响

- 入库不阻塞 API：上传接口 202 + `document.status='processing'`，前端轮询状态（M2 的 C1 验收即此链路）。
- 同步实现让"入库失败重试与断点续传"容易验证：M2 的 C5 实测重跑后 chunks 数量不变。
