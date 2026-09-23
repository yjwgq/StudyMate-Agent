# Personal Agent OS

面向个人用户的**多用户 AI 助理平台**：Agent 运行时 · 长期记忆 · 生产级 RAG · MCP 工具生态 · 全链路可观测。

前置身是 StudyMate Agent（单机、单用户、Chroma 单路检索的学习问答 demo），本项目是其工程化演进版本。

## 当前进度

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M0 | 端到端最小闭环（浏览器 → FastAPI → LLM → SSE 逐字回流） | ✅ 完成（2026-09-22 验收通过） |
| M1 | 基础设施与多租户认证（compose 全量编排 / 16 张表迁移 / JWT+RLS / 会话锁 / 幂等） | ✅ 完成（2026-09-23 验收 B1–B8 通过） |
| M2 | 知识库入库管线（上传校验 / PyMuPDF 解析 / 父子分块 / 千问向量化 / Celery ingest） | ✅ 完成（2026-09-23 验收 C1–C7 通过） |
| M3 | 单路 RAG + 可观测 + 评测基建 | ⏭️ 下一个 |
| M4 – M10 | 见施工计划 | ⏳ 未开始 |

## 文档

| 文档 | 用途 |
|------|------|
| [`项目实施计划.md`](./项目实施计划.md) | **施工依据**：里程碑、任务分解、验收标准、进度追踪 |
| [`docs/design/PersonalAgent_设计文档_v1.1.md`](./docs/design/PersonalAgent_设计文档_v1.1.md) | 设计依据：架构、ADR、DDL、协议 |
| [`docs/design/PersonalAgent_设计文档_评审报告.md`](./docs/design/PersonalAgent_设计文档_评审报告.md) | 设计评审：45 条问题与改进方案 |

## 快速开始（M1）

### 1. 准备环境变量

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，**M0 必填**：`LLM_API_KEY` / `LLM_MODEL`（注意与 `LLM_BASE_URL` 同源）；**M1 必填**：

- `POSTGRES_PASSWORD`：数据库密码（自定）
- 三条 `DATABASE_URL*` 中的 `CHANGE_ME` → 替换为上面同一个密码值
- `JWT_SECRET`：`openssl rand -hex 32` 生成（PowerShell 可用 Git Bash 或 WSL 执行）

### 2. 启动

```powershell
docker compose -f infra/docker-compose.dev.yml up -d --build
```

服务链：postgres → migrate（alembic + checkpoint 建表）→ api（等迁移成功才启动）→ caddy/web。

### 3. 访问

| 地址 | 用途 |
|------|------|
| http://localhost:8080 | 应用（未登录会跳转 /login） |
| http://localhost:8080/api/v1/health | 存活检查 |
| http://localhost:8080/api/v1/ready | 就绪检查（LLM / JWT / DB / Redis） |
| http://localhost:8080/api/v1/config | 查看当前配置（不回显密钥） |

### 4. 跑测试

```powershell
uv sync
uv run pytest tests/unit -v          # 单元测试（无需数据库）
uv run pytest tests/security -v      # 安全集成测试（需要开发栈已启动）
```

### 5. 仅启动后端（调试用）

```powershell
uv sync
uv run uvicorn apps.api.main:app --reload --port 8000
```

## 目录结构

完整结构见 [`项目实施计划.md` §3.2](./项目实施计划.md)。核心分层：

```
apps/api/      FastAPI 接入层（core / api.v1 / repositories）
apps/web/      Next.js 前端（聊天 / 登录 / 注册）
agent/         Agent 运行时（图编排、工具、记忆、检索、护栏）
mcp_servers/   自建 MCP 工具服务
worker/        Celery 异步任务
infra/         容器编排、镜像、迁移（alembic）、数据库初始化
evals/         评测集与评测脚本
scripts/       一次性脚本（checkpoint 建表等）
```

## 技术栈

Python 3.12 · FastAPI · SQLAlchemy/asyncpg · Alembic · Redis · LangGraph · PostgreSQL 16 + pgvector · Next.js 15 · Docker Compose

**推理全程走云端**（不部署本地模型）：

| 角色 | 服务商 |
|------|--------|
| LLM | DeepSeek / 智谱（OpenAI 兼容，以 `.env` 配置为准） |
| Embedding | 通义千问（DashScope 兼容模式，1024 维） |
| Reranker | bge-reranker-v2-m3 |
