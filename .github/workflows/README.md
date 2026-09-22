GitHub Actions 工作流目录。

规划（设计文档 v1.1 §15.2）：
    ci.yml   PR 触发：ruff + mypy + pytest（含 security 用例）
             + 前端 build + 60 条评测冒烟（阈值基于噪声地板 2σ）
    deploy.yml   main 触发：构建镜像、打 tag、可选部署

待 M7 落地评测门禁后创建 ci.yml。
