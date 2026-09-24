GitHub Actions 工作流目录。

    ci.yml       PR / push 触发：后端（ruff + mypy + 单测）、前端（tsc + build）、
                 迁移可加载性 —— 只跑**不依赖本地栈**的检查（M7-4）

刻意不放进 CI 的（设计决策，见 M7 验收手册）：
    · tests/security（需要 PostgreSQL + Redis）—— 本地人工跑
    · tests/integration（Agent 全图，需栈）—— 本地人工跑
    · 评测冒烟（真实语料 + 模型额度，慢且烧钱）—— 本地人工跑，报告归档到 evals/reports/
    · deploy.yml（构建镜像 / 部署）—— 未纳入 MVP 范围（本地 compose 已足够）
