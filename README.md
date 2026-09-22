# Personal Agent OS

面向个人用户的**多用户 AI 助理平台**：Agent 运行时 · 长期记忆 · 生产级 RAG · MCP 工具生态 · 全链路可观测。

前置身是 StudyMate Agent（单机、单用户、Chroma 单路检索的学习问答 demo），本项目是其工程化演进版本。

## 当前进度

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M0 | 端到端最小闭环（浏览器 → FastAPI → LLM → SSE 逐字回流） | ✅ 完成（2026-09-22 验收通过） |
| M1 – M10 | 见施工计划 | 🚧 M1 进行中 |

## 文档

| 文档 | 用途 |
|------|------|
| [`项目实施计划.md`](./项目实施计划.md) | **施工依据**：里程碑、任务分解、验收标准、进度追踪 |
| [`docs/design/PersonalAgent_设计文档_v1.1.md`](./docs/design/PersonalAgent_设计文档_v1.1.md) | 设计依据：架构、ADR、DDL、协议 |
| [`docs/design/PersonalAgent_设计文档_评审报告.md`](./docs/design/PersonalAgent_设计文档_评审报告.md) | 设计评审：45 条问题与改进方案 |

## 快速开始（M0）

### 1. 准备环境变量

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，**至少填写这两项**：

```ini
LLM_API_KEY=<你的 DeepSeek API Key>
LLM_MODEL=<登录控制台确认的当前可用模型名>
```

> `.env` 已被 `.gitignore` 忽略，不会进入版本库。

### 2. 启动

```powershell
docker compose -f infra/docker-compose.dev.yml up --build
```

首次启动需要拉取镜像并安装依赖，耗时较长属正常。

### 3. 访问

| 地址 | 用途 |
|------|------|
| http://localhost:8080 | 聊天页 |
| http://localhost:8080/api/v1/health | 存活检查 |
| http://localhost:8080/api/v1/ready | 就绪检查（含 LLM 配置自检） |
| http://localhost:8080/api/v1/config | 查看当前配置（不回显密钥） |

### 4. 仅启动后端（调试用）

```powershell
uv sync
uv run uvicorn apps.api.main:app --reload --port 8000
```

## 目录结构

完整结构见 [`项目实施计划.md` §3.2](./项目实施计划.md)。核心分层：

```
apps/api/      FastAPI 接入层
apps/web/      Next.js 前端
agent/         Agent 运行时（图编排、工具、记忆、检索、护栏）
mcp_servers/   自建 MCP 工具服务
worker/        Celery 异步任务
infra/         容器编排、镜像、迁移
evals/         评测集与评测脚本
```

## 技术栈

Python 3.12 · FastAPI · LangGraph · PostgreSQL 16 + pgvector · Redis · Celery · Next.js 15 · Docker Compose

**推理全程走云端**（不部署本地模型）：

| 角色 | 服务商 |
|------|--------|
| LLM | DeepSeek |
| Embedding | 通义千问（DashScope 兼容模式，1024 维） |
| Reranker | bge-reranker-v2-m3 |
