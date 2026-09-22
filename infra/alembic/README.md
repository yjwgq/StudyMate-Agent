Alembic 迁移目录 —— 由 M1 初始化。

    alembic.ini
    env.py
    versions/   16 张业务表的建表脚本

约定：
    1. 迁移由**独立的 migrate 步骤**执行，不要让多个容器启动时各自 upgrade
       （并发迁移会冲突）。
    2. 所有 DDL 必须能在 CI 中对着真实 Postgres 跑通一遍 —— 这是 F45 的教训：
       设计文档里一个 `vector_cosine_factor_ops` 拼写错误，会让建库第一步就失败。
