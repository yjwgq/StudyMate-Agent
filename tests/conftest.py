"""顶层测试环境：在导入任何 apps.* 之前固化测试所需的最小配置。

pydantic-settings 的优先级是 环境变量 > .env；这里只兜底缺失项，
不覆盖用户已有的环境变量。security 集成测试的 DB/Redis 地址由
tests/security/conftest.py 覆盖为宿主机映射端口。
"""

import os

# JWT_SECRET 兜底：保证单测里 token 签发/校验可用且进程内一致
os.environ.setdefault("JWT_SECRET", "unit-test-secret-do-not-use-in-prod")
# LLM 配置兜底：聊天集成测试一律用 fake 客户端，不真调模型
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")
