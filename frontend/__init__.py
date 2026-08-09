# -*- coding: utf-8 -*-
"""
frontend 模块：Streamlit 交互前端
==================================
支持两种运行模式：
1. 一体化模式（integrated）：Streamlit 直接导入 graph 工作流，无需启动后端；
2. 前后端分离模式（separated）：前端通过 requests 调用 FastAPI 接口。

模式切换：在 .env 中设置 APP_MODE=integrated 或 APP_MODE=separated
        （也可在 Streamlit 侧边栏运行时切换）。

依据：项目硬性规则——前端 Streamlit 1.40.0，后端 FastAPI 按需封装。
"""

__all__ = ["api_client", "workflow_bridge"]
__version__ = "1.0.0"
