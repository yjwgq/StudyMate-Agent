# -*- coding: utf-8 -*-
"""
frontend/views/system_check_page.py — 系统检测页
================================================
职责：
1. 可视化展示环境自检结果（基于 env_check.py + 后端 /api/health）；
2. 包含 Ollama 状态、模型可用性、Chroma 目录与可打开性、锁文件检测；
3. 提供一键修复指引（Ollama 离线、Chroma 锁冲突等）；
4. 提供端口占用快速排查工具。

依据：项目硬性规则——系统检测页可视化展示 env_check.py 自检结果，
      包含 Chroma 数据库状态。
"""
from __future__ import annotations

import subprocess
from typing import List, Tuple

import streamlit as st

from frontend.error_utils import show_error_popup


# ---------------------------------------------------------------------------
# 工具：本地端口检测
# ---------------------------------------------------------------------------

def _check_port(port: int) -> bool:
    """检测端口是否被占用（True=占用）。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("localhost", port))
            return False  # 未占用
        except OSError:
            return True  # 被占用


# ---------------------------------------------------------------------------
# 渲染单条检查项（带颜色图标）
# ---------------------------------------------------------------------------

def _render_check_item(name: str, passed: bool, message: str) -> None:
    """渲染单条检查项。"""
    icon = "✅" if passed else "❌"
    color = "green" if passed else "red"
    st.markdown(
        f"<span style='color:{color}'>{icon}</span> **{name}** — {message}",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# 主渲染函数
# ---------------------------------------------------------------------------

def render(backend) -> None:
    """渲染系统检测页。"""
    st.markdown("# 🩺 系统检测")
    st.caption("可视化展示模型模式 / Ollama / 云端API / Chroma / 端口 / 依赖等环境状态")

    # ---- 顶部：触发检测按钮 ----
    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        if st.button("🔄 立即检测", type="primary", use_container_width=True):
            st.session_state.last_health_result = None  # 强制刷新
    with col_info:
        st.caption("检测将调用后端 /api/health，包含 Ollama、Chroma、配置摘要。")

    # ---- 执行检测 ----
    if st.session_state.get("last_health_result") is None:
        with st.spinner("⏳ 正在执行环境检测..."):
            resp = backend.health()
            if isinstance(resp, dict) and resp.get("success", False):
                st.session_state.last_health_result = resp.get("data") or {}
            else:
                st.session_state.last_health_result = {"_error": resp}

    health = st.session_state.last_health_result or {}

    # 错误处理
    if "_error" in health:
        show_error_popup(health["_error"])
        st.info("请按错误提示修复后重试。")
        return

    # ---- 整体状态徽章 ----
    status = health.get("status", "unknown")
    status_map = {
        "healthy": ("🟢 健康", "green", "所有核心服务可用"),
        "degraded": ("🟡 降级", "orange", "部分服务不可用，功能受限"),
        "unhealthy": ("🔴 异常", "red", "核心服务不可用，请修复"),
        "unknown": ("⚪ 未知", "gray", "未执行检测"),
    }
    badge, color, desc = status_map.get(status, status_map["unknown"])
    st.markdown(
        f"<div style='padding:16px;border-radius:8px;"
        f"background:{color}22;border-left:5px solid {color};'>"
        f"<h2 style='color:{color};margin:0;'>{badge}</h2>"
        f"<p style='color:#555;margin:4px 0 0 0;'>{desc}</p>"
        f"</div>",
        unsafe_allow_html=True,
    )

    st.divider()

    # ---- 三栏：模型模式 / Ollama / 云端API ----
    is_cloud = health.get("config", {}).get("use_cloud_model", False)
    if is_cloud:
        col_model, col_chroma = st.columns(2)
    else:
        col_ollama, col_chroma = st.columns(2)

    if is_cloud:
        with col_model:
            st.markdown("### ☁️ 云端API配置")
            cloud = health.get("cloud") or {}
            api_key_configured = cloud.get("api_key_configured", False)
            llm_base_url = cloud.get("llm_base_url", "")
            llm_model_name = cloud.get("llm_model_name", "")
            embed_model_name = cloud.get("embed_model_name", "")
            
            _render_check_item("API密钥配置", api_key_configured, "已配置" if api_key_configured else "未配置或使用默认值")
            _render_check_item("LLM接口地址", bool(llm_base_url and llm_base_url != "https://api.example.com/v1"), llm_base_url or "未配置")
            _render_check_item("LLM模型名称", bool(llm_model_name), llm_model_name or "未配置")
            _render_check_item("嵌入模型名称", bool(embed_model_name), embed_model_name or "未配置")
            
            # 查找云端连通性检查结果
            checks = health.get("checks", [])
            cloud_reachable = False
            for c in checks:
                if c.get("name") == "云端API连通性":
                    cloud_reachable = c.get("passed", False)
                    _render_check_item("云端API连通性", cloud_reachable, c.get("message", ""))
                    break
            
            with st.expander("💡 云端模式提示"):
                st.markdown(
                    "当前使用云端API模型，无需启动本地Ollama服务。\n\n"
                    "切换到本地模式：修改 .env 中 USE_CLOUD_MODEL=false"
                )
    else:
        with col_ollama:
            st.markdown("### 🦙 Ollama 服务")
            ollama = health.get("ollama") or {}
            reachable = ollama.get("reachable", False)
            host = ollama.get("host", "")
            models = ollama.get("models") or []
            required = ollama.get("required") or []

            _render_check_item("Ollama 连通性", reachable, f"{host} {'在线' if reachable else '离线'}")

            for model in required:
                present = model in models
                _render_check_item(
                    f"模型 {model}",
                    present,
                    "已拉取" if present else f"未拉取（ollama pull {model}）",
                )

            if models:
                with st.expander(f"已安装模型列表（{len(models)} 个）"):
                    for m in models:
                        st.code(m)

    with col_chroma:
        st.markdown("### 🗄️ Chroma 向量库")
        chroma = health.get("chroma") or {}
        exists = chroma.get("exists", False)
        openable = chroma.get("openable", False)
        doc_count = chroma.get("doc_count", 0)
        lock_files = chroma.get("lock_files") or []
        chroma_path = chroma.get("persist_directory", "")
        chroma_err = chroma.get("error")

        _render_check_item("持久化目录", exists, f"{chroma_path}")
        _render_check_item(
            "数据库可打开",
            openable,
            f"文档数≈{doc_count}" if openable else f"失败：{chroma_err}",
        )
        _render_check_item(
            "锁文件检测",
            len(lock_files) == 0,
            f"无锁文件" if not lock_files else f"检测到 {len(lock_files)} 个 .lock",
        )

        if lock_files:
            with st.expander(f"⚠️ 锁文件列表（{len(lock_files)} 个）"):
                for f in lock_files[:10]:
                    st.code(f, language="text")
                st.info(
                    "解决方案：关闭其他写入进程后执行\n"
                    "`Remove-Item ./chroma_db/*.lock -Force -ErrorAction SilentlyContinue`"
                )

    st.divider()

    # ---- 逐项明细 ----
    st.markdown("### 📋 检查明细")
    checks = health.get("checks") or []
    if checks:
        for c in checks:
            _render_check_item(
                c.get("name", ""),
                c.get("passed", False),
                c.get("message", ""),
            )
    else:
        st.info("无检查明细。")

    st.divider()

    # ---- 配置摘要 ----
    st.markdown("### ⚙️ 配置摘要")
    cfg = health.get("config") or {}
    if cfg:
        cfg_cols = st.columns(len(cfg) if cfg else 1)
        for i, (k, v) in enumerate(cfg.items()):
            with cfg_cols[i % len(cfg_cols)]:
                st.metric(label=k, value=str(v))

    st.divider()

    # ---- 端口占用快速排查 ----
    st.markdown("### 🔌 端口占用排查")
    st.caption("检测 8000（FastAPI）/ 8501（Streamlit）/ 11434（Ollama）端口占用情况")
    ports = [8000, 8501, 11434]
    port_cols = st.columns(len(ports))
    for i, port in enumerate(ports):
        with port_cols[i]:
            occupied = _check_port(port)
            label = {8000: "FastAPI", 8501: "Streamlit", 11434: "Ollama"}[port]
            if occupied:
                # 占用可能是服务正在运行，也可能是冲突
                st.warning(f"端口 {port}（{label}）\n状态：被占用 ✅运行中 / ⚠️冲突")
            else:
                st.success(f"端口 {port}（{label}）\n状态：空闲")

    with st.expander("🛠️ 端口冲突解决命令"):
        st.code(
            "# 查看 8000 端口占用进程\n"
            "netstat -ano | findstr :8000\n\n"
            "# 按 PID 结束进程（替换 <PID>）\n"
            "taskkill /PID <PID> /F\n\n"
            "# 一次性结束所有占用 8000 的进程（PowerShell）\n"
            "Get-NetTCPConnection -LocalPort 8000 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }",
            language="powershell",
        )

    st.divider()

    # ---- 修复指引 ----
    st.markdown("### 💡 常见问题修复指引")
    with st.expander("🦙 Ollama 离线", expanded=False):
        st.markdown(
            "1. 启动 Ollama 服务：`ollama serve`\n"
            "2. 拉取模型：`ollama pull <LLM模型名>` 与 `ollama pull <嵌入模型名>`（参见 .env 配置）\n"
            "3. 确认 .env：`OLLAMA_HOST=http://localhost:11434`\n"
            "4. 浏览器访问 http://localhost:11434 验证"
        )
    with st.expander("🔒 Chroma 文件锁冲突", expanded=False):
        st.markdown(
            "1. 关闭其他正在写入 Chroma 的进程（如 batch_build_kb.py、其他 Streamlit 实例）；\n"
            "2. 任务管理器结束残留 python.exe；\n"
            "3. 删除 .lock 文件：`Remove-Item ./chroma_db/*.lock -Force -ErrorAction SilentlyContinue`\n"
            "4. 重启 FastAPI 与 Streamlit；\n"
            "5. 仍失败：备份 chroma_db 后清空目录重建知识库。"
        )
    with st.expander("🔌 端口被占用", expanded=False):
        st.markdown(
            "1. `netstat -ano | findstr :8000` 查看占用 PID；\n"
            "2. `taskkill /PID <PID> /F` 结束进程；\n"
            "3. 或修改 .env：`FASTAPI_PORT=8010` / `STREAMLIT_PORT=8502`。"
        )
    with st.expander("📥 依赖未安装", expanded=False):
        st.markdown(
            "1. 执行 `uv sync` 安装 pyproject.toml 中所有依赖；\n"
            "2. 单独安装：`uv add fastapi==0.140.0 streamlit==1.40.0`；\n"
            "3. 验证：`uv run python env_check.py`。"
        )
