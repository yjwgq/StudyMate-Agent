# -*- coding: utf-8 -*-
"""
frontend/views — Streamlit 子页面模块
=====================================
4 大导航页面对应模块：
- chat_page.py         智能问答页
- upload_page.py       知识库上传页
- literature_page.py   文献整理页
- system_check_page.py 系统检测页

每个页面模块导出 render(backend) 函数，由 app.py 调度。
"""

__all__ = [
    "chat_page",
    "upload_page",
    "literature_page",
    "system_check_page",
]
