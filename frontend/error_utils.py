# -*- coding: utf-8 -*-
"""
frontend/error_utils.py — 前端统一错误展示工具
=============================================
职责：
1. 解析后端返回的 {success, error} 结构，识别 Ollama 离线 / Chroma 锁文件等错误码；
2. 在 Streamlit 中以友好弹窗（st.error / st.warning）形式展示；
3. 提供 Chroma 锁冲突的解决方案提示。

依据：项目硬性规则——增加 Ollama 离线、Chroma 文件锁异常友好弹窗提示。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import streamlit as st


# 错误码 → 友好提示 + 解决方案
ERROR_SOLUTIONS = {
    "OLLAMA_OFFLINE": {
        "title": "🚫 Ollama 服务未启动",
        "solution": (
            "请按以下步骤排查：\n"
            "1. 打开命令行执行 `ollama serve` 启动服务；\n"
            "2. 拉取模型：`ollama pull <LLM模型名>` 与 `ollama pull <嵌入模型名>`（参见 .env 配置）；\n"
            "3. 确认 .env 中 `OLLAMA_HOST=http://localhost:11434`；\n"
            "4. 浏览器访问 http://localhost:11434 确认服务可达。"
        ),
    },
    "CLOUD_AUTH_FAILED": {
        "title": "🔐 云端API认证失败",
        "solution": (
            "云端API密钥配置错误，请检查：\n"
            "1. .env 中 CLOUD_API_KEY 是否正确填写；\n"
            "2. API Key 是否过期或已被撤销；\n"
            "3. 服务商是否要求绑定 IP 或其他认证；\n"
            "4. 切换到本地模式：修改 .env 中 USE_CLOUD_MODEL=false。"
        ),
    },
    "CLOUD_NETWORK_ERROR": {
        "title": "🌐 云端API网络连接失败",
        "solution": (
            "无法连接云端API服务器，请检查：\n"
            "1. 网络连接是否正常，能否访问互联网；\n"
            "2. .env 中 CLOUD_LLM_BASE_URL 地址是否正确；\n"
            "3. 防火墙/代理是否阻止了API请求；\n"
            "4. 切换到本地模式：修改 .env 中 USE_CLOUD_MODEL=false。"
        ),
    },
    "CLOUD_QUOTA_EXCEEDED": {
        "title": "💰 云端API额度耗尽",
        "solution": (
            "云端API调用额度已用完或频率超限，请：\n"
            "1. 登录服务商控制台查看额度状态；\n"
            "2. 等待额度恢复（通常每日/每月刷新）；\n"
            "3. 升级套餐或购买更多额度；\n"
            "4. 切换到本地模式：修改 .env 中 USE_CLOUD_MODEL=false。"
        ),
    },
    "CHROMA_LOCK": {
        "title": "🔒 Chroma 数据库被锁定",
        "solution": (
            "Windows 下 Chroma 文件锁冲突解决方案：\n"
            "1. 关闭其他正在写入 Chroma 的进程（如 batch_build_kb.py）；\n"
            "2. 任务管理器结束所有残留 python.exe 进程；\n"
            "3. 删除 chroma_db 目录下的 .lock 文件：\n"
            "   PowerShell: `Remove-Item ./chroma_db/*.lock -Force -ErrorAction SilentlyContinue`\n"
            "4. 重启 Streamlit 与 FastAPI 服务；\n"
            "5. 仍失败可备份后清空 chroma_db 目录重建知识库。"
        ),
    },
    "CHROMA_FAILED": {
        "title": "⚠️ Chroma 向量库读写失败",
        "solution": (
            "请检查：\n"
            "1. chroma_db 目录是否存在且可读写；\n"
            "2. 是否有进程占用（见 Chroma 锁文件冲突方案）；\n"
            "3. 磁盘空间是否充足；\n"
            "4. 必要时备份后删除 chroma_db 重建。"
        ),
    },
    "TIMEOUT": {
        "title": "⏱️ 请求处理超时",
        "solution": (
            "请尝试：\n"
            "1. 简化问题或缩减输入长度；\n"
            "2. 等待 30 秒后重试；\n"
            "3. 在 .env 中调大 REQUEST_TIMEOUT（如 180）；\n"
            "4. 关闭其他占用 CPU/内存的进程；\n"
            "5. 云端模式下可尝试切换到本地模式。"
        ),
    },
    "BACKEND_OFFLINE": {
        "title": "🔌 后端服务未启动",
        "solution": (
            "前后端分离模式下，请先启动 FastAPI 后端：\n"
            "1. 双击 start_server.bat 启动后端；\n"
            "2. 或执行 `uv run uvicorn api.main:app --port 8000`；\n"
            "3. 访问 http://localhost:8000/docs 确认接口可用；\n"
            "4. 若想跳过后端，请在侧边栏切换为'一体化模式'。"
        ),
    },
    "VALIDATION": {
        "title": "📝 参数校验失败",
        "solution": "请检查输入参数是否符合接口要求（如非空、长度限制）。",
    },
    "INTERNAL": {
        "title": "💥 服务内部错误",
        "solution": (
            "请查看后端日志（logs/ 目录）定位具体异常，\n"
            "或将错误详情提交给开发者。"
        ),
    },
}


def show_error_popup(resp: Dict[str, Any]) -> None:
    """
    根据后端响应展示友好错误弹窗。

    Args:
        resp: 后端返回的 {success, data, error, elapsed} 字典
    """
    if not isinstance(resp, dict):
        st.error(f"❌ 后端返回格式异常：{resp}")
        return

    if resp.get("success", False):
        return  # 成功无需提示

    err = resp.get("error") or {}
    code = err.get("code", "UNKNOWN") if isinstance(err, dict) else "UNKNOWN"
    message = err.get("message", "未知错误") if isinstance(err, dict) else str(err)
    detail = err.get("detail") if isinstance(err, dict) else None

    solution = ERROR_SOLUTIONS.get(code, {
        "title": f"❌ 错误：{code}",
        "solution": message,
    })

    # 主错误提示
    st.error(f"{solution['title']}\n\n{message}")

    # 解决方案（可折叠）
    with st.expander("💡 解决方案", expanded=True):
        st.markdown(solution["solution"])

    # 调试详情（默认折叠）
    if detail:
        with st.expander("🔧 调试详情", expanded=False):
            st.code(detail, language="text")


def show_success_message(msg: str = "操作成功！") -> None:
    """统一成功提示。"""
    st.success(msg)


def check_backend_online(backend) -> bool:
    """
    检查后端是否在线（仅分离模式有意义）。

    Args:
        backend: ApiClient 或 WorkflowBridge 实例

    Returns:
        True 在线 / False 离线
    """
    try:
        return backend.ping()
    except Exception:
        return False
