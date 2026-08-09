# -*- coding: utf-8 -*-
"""
frontend/app.py — Streamlit 主入口
===================================
职责：
1. 渲染主页面框架（标题、侧边栏导航、运行模式切换）；
2. 调度 4 大子页面：智能问答 / 知识库上传 / 文献整理 / 系统检测；
3. 根据 APP_MODE 自动选择后端（一体化 WorkflowBridge / 分离 ApiClient）；
4. 全局会话状态管理（聊天历史、检索片段、运行模式等）。

启动方式：
- 前后端分离：双击 start_front.bat （需先启动 start_server.bat）
- 一体化模式：双击 start_integrated.bat
- 命令行：uv run streamlit run frontend/app.py --server.port 8501

依据：项目硬性规则——前端 Streamlit 1.40.0，UI 简洁适配学生使用。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 项目根路径注入（保证 api / graph / tools 模块可导入）
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st

from dotenv import load_dotenv
load_dotenv(dotenv_path=str(Path(_PROJECT_ROOT) / ".env"))

from api.config import settings
from frontend.workflow_bridge import get_backend, workflow_bridge
from frontend.error_utils import show_error_popup, check_backend_online


# ===========================================================================
# 一、页面全局配置
# ===========================================================================

st.set_page_config(
    page_title="StudyMate Agent — 智能学习助手",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "About": (
            "StudyMate Agent：基于 LangGraph+Ollama+Chroma 的多 Agent 智能学习助手。"
            "支持智能问答、文献整理、知识库管理与系统自检。"
        )
    },
)


# ===========================================================================
# 二、全局样式注入
# ===========================================================================

def inject_custom_css() -> None:
    """注入自定义 CSS，美化整体界面。"""
    st.markdown("""
    <style>
    /* ---- 侧边栏美化 ---- */
    /* 侧边栏背景渐变 */
    section[data-testid="stSidebar"] > div:first-child {
        background: linear-gradient(180deg, #f0f4ff 0%, #fafbff 100%);
    }
    /* 侧边栏内边距 */
    section[data-testid="stSidebar"] .stMarkdown {
        padding-top: 0.3rem;
    }
    /* 导航 radio 按钮美化 */
    section[data-testid="stSidebar"] .stRadio > label {
        font-weight: 600;
        font-size: 0.95rem;
    }
    section[data-testid="stSidebar"] .stRadio > div[role="radiogroup"] label {
        padding: 0.4rem 0.2rem;
        border-radius: 8px;
        transition: background-color 0.2s;
    }
    section[data-testid="stSidebar"] .stRadio > div[role="radiogroup"] label:hover {
        background-color: #e8eef9;
    }
    section[data-testid="stSidebar"] .stRadio > div[role="radiogroup"] label[data-checked="true"] {
        background-color: #dbe5ff;
        font-weight: 700;
    }

    /* ---- 主区域美化 ---- */
    /* 主内容区最大宽度 */
    .stApp > section > div > div {
        max-width: 1000px;
        margin: 0 auto;
    }
    /* 标题底部间距 */
    h1, h2, h3 {
        padding-bottom: 0.3rem;
    }
    /* 卡片式容器 */
    .stExpander {
        border: 1px solid #e0e6ed;
        border-radius: 10px;
        overflow: hidden;
    }
    /* 按钮圆角 */
    .stButton > button {
        border-radius: 8px;
        font-weight: 500;
    }
    /* 成功/错误提示圆角 */
    .stAlert {
        border-radius: 10px;
    }
    /* 分割线淡化 */
    hr {
        border-color: #e8ecf2;
        margin: 0.8rem 0;
    }
    /* 隐藏 Streamlit 默认页面选择器（英文导航） */
    nav[aria-label="Pages"] {
        display: none !important;
    }
    </style>
    """, unsafe_allow_html=True)


inject_custom_css()


# ===========================================================================
# 三、会话状态初始化
# ===========================================================================

def init_session_state() -> None:
    """初始化全局会话状态（聊天历史、检索片段、运行模式等）。"""
    defaults = {
        # 聊天历史：[{role, content, retrieval_chunks, execution_log}, ...]
        "chat_history": [],
        # 当前运行模式（前后端分离/一体化）
        "app_mode": os.getenv("APP_MODE", "separated").lower(),
        # 知识库上传结果缓存
        "last_upload_result": None,
        # 上次健康检测缓存
        "last_health_result": None,
        # LLM 温度（智能问答页可调）
        "temperature": 0.7,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


init_session_state()


# ===========================================================================
# 四、侧边栏：功能导航 + 模型模式 + 运行模式
# ===========================================================================

def render_sidebar() -> None:
    """渲染侧边栏：功能导航（置顶）、模型模式、运行模式、后端状态。"""
    with st.sidebar:
        # ---- 标题区 ----
        st.markdown("## 🎓 StudyMate Agent")
        st.caption("多 Agent 智能学习助手")
        st.divider()

        # ---- 功能导航（置顶） ----
        st.markdown("### 📚 功能导航")
        page_options = [
            "💬 智能问答",
            "📤 知识库上传",
            "📄 文献整理",
            "🩺 系统检测",
        ]
        if "current_page" not in st.session_state:
            st.session_state.current_page = page_options[0]
        selected_page = st.radio(
            "选择页面",
            options=page_options,
            index=page_options.index(st.session_state.current_page),
            label_visibility="collapsed",
        )
        st.session_state.current_page = selected_page

        st.divider()

        # ---- 模型模式展示 ----
        is_cloud = settings.use_cloud_model
        model_mode_label = "☁️ 云端API模型" if is_cloud else "🦙 本地Ollama模型"
        st.markdown(f"**{model_mode_label}**")

        with st.expander("📋 模型配置详情", expanded=False):
            if is_cloud:
                st.markdown("**云端配置：**")
                st.text(f"LLM模型: {settings.cloud_llm_model_name or '未配置'}")
                st.text(f"嵌入模型: {settings.cloud_embed_model_name or '未配置'}")
                st.text(f"API地址: {settings.cloud_llm_base_url or '未配置'}")
                st.text(f"密钥状态: {'已配置' if settings.cloud_api_key and settings.cloud_api_key != 'your-cloud-api-key' else '未配置'}")
            else:
                st.markdown("**本地配置：**")
                st.text(f"LLM模型: {settings.ollama_llm_model}")
                st.text(f"嵌入模型: {settings.ollama_embed_model}")
                st.text(f"Ollama地址: {settings.ollama_host}")

        st.caption("💡 修改 .env 中 USE_CLOUD_MODEL 切换模式")
        st.divider()

        # ---- 运行模式切换 ----
        st.markdown("#### ⚙️ 运行模式")
        mode_options = {
            "separated": "前后端分离（推荐）",
            "integrated": "一体化模式（免后端）",
        }
        current_mode = st.session_state.app_mode
        current_label = mode_options.get(current_mode, mode_options["separated"])

        selected_label = st.radio(
            "选择运行模式",
            options=list(mode_options.values()),
            index=list(mode_options.values()).index(current_label),
            help=(
                "前后端分离：Streamlit 调用 FastAPI 接口，需先启动后端；\n"
                "一体化模式：Streamlit 直接调用工作流，无需后端。"
            ),
            label_visibility="collapsed",
        )
        new_mode = next(k for k, v in mode_options.items() if v == selected_label)
        if new_mode != st.session_state.app_mode:
            st.session_state.app_mode = new_mode
            st.rerun()

        # ---- 后端状态指示 ----
        backend = get_backend(st.session_state.app_mode)
        if st.session_state.app_mode == "separated":
            online = check_backend_online(backend)
            if online:
                st.success(f"✅ 后端在线\n`{backend.base_url}`")
            else:
                st.error(
                    f"❌ 后端离线\n`{backend.base_url}`\n\n"
                    "请先启动 FastAPI 后端，或切换为一体化模式。"
                )
        else:
            st.info("🔗 一体化模式：直接调用 graph 工作流")

        st.divider()

        # ---- 环境信息 ----
        with st.expander("ℹ️ 环境信息"):
            if is_cloud:
                st.text(f"LLM: {settings.cloud_llm_model_name or '未配置'}")
                st.text(f"Embed: {settings.cloud_embed_model_name or '未配置'}")
            else:
                st.text(f"LLM: {settings.ollama_llm_model}")
                st.text(f"Embed: {settings.ollama_embed_model}")
            st.text(f"Chroma: {settings.chroma_persist_directory}")
            st.text(f"FastAPI: {settings.fastapi_port}")
            st.text(f"Streamlit: {settings.streamlit_port}")


# ===========================================================================
# 五、主区域：页面调度
# ===========================================================================

def render_main() -> None:
    """根据侧边栏选择渲染对应页面。"""
    page = st.session_state.current_page
    # 获取当前后端
    backend = get_backend(st.session_state.app_mode)

    if page == "💬 智能问答":
        from frontend.views import chat_page
        chat_page.render(backend)
    elif page == "📤 知识库上传":
        from frontend.views import upload_page
        upload_page.render(backend)
    elif page == "📄 文献整理":
        from frontend.views import literature_page
        literature_page.render(backend)
    elif page == "🩺 系统检测":
        from frontend.views import system_check_page
        system_check_page.render(backend)
    else:
        st.warning("未知页面")


# ===========================================================================
# 六、主入口
# ===========================================================================

def main() -> None:
    """Streamlit 主入口。"""
    render_sidebar()
    render_main()


if __name__ == "__main__":
    main()
