# -*- coding: utf-8 -*-
"""
api 模块：FastAPI 后端接口
=========================
封装 4 个核心接口供前端（Streamlit）或第三方调用：
- POST /api/chat      完整多 Agent 问答流程
- POST /api/retrieve  单独 Chroma 向量检索
- POST /api/calc      独立数理代码计算（RestrictedPython 沙箱）
- GET  /api/health    环境健康检测（含 Chroma 数据库状态）

依据：项目硬性规则——后端 FastAPI 按需封装，模型层全部通过 Ollama 调用，
      向量检索基于 langchain-chroma，数理计算走 RestrictedPython+SymPy 沙箱。
"""
from api.config import settings, Settings  # noqa: F401

__all__ = ["settings", "Settings"]
__version__ = "1.0.0"
