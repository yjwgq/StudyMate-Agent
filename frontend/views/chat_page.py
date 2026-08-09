# -*- coding: utf-8 -*-
"""
frontend/views/chat_page.py — 智能问答页
========================================
职责：
1. 渲染聊天历史，支持多轮对话；
2. 调用 backend.chat() 触发多 Agent 工作流；
3. 展示 Agent 流转日志（可折叠）；
4. 展示 Chroma 召回原文片段（可折叠）；
5. 提供 temperature 调节滑块；
6. 支持导出当前对话为 Markdown 文件。

依据：项目硬性规则——前端 UI 简洁适配学生使用，代码注释完整。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

import streamlit as st

from frontend.error_utils import show_error_popup


# ---------------------------------------------------------------------------
# 工具：导出对话为 Markdown
# ---------------------------------------------------------------------------

def _build_markdown(history: List[Dict[str, Any]]) -> str:
    """将聊天历史导出为 Markdown 字符串。"""
    lines = [
        f"# StudyMate Agent 对话记录",
        f"",
        f"导出时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
        f"---",
        f"",
    ]
    for i, item in enumerate(history, 1):
        role = item.get("role", "user")
        content = item.get("content", "")
        if role == "user":
            lines.append(f"## 🧑 用户 #{i}")
        else:
            lines.append(f"## 🤖 助手 #{i}")
        lines.append("")
        lines.append(content)
        lines.append("")

        # 附带召回片段
        chunks = item.get("retrieval_chunks") or []
        if chunks:
            lines.append(f"<details><summary>📎 Chroma 召回片段 ({len(chunks)} 条)</summary>")
            lines.append("")
            for j, c in enumerate(chunks, 1):
                lines.append(f"**片段 {j}** (score={c.get('score', 0):.4f})")
                lines.append("")
                lines.append("```")
                lines.append(c.get("content", "")[:500])
                lines.append("```")
                lines.append("")
            lines.append("</details>")
            lines.append("")

        # 附带流转日志
        logs = item.get("execution_log") or []
        if logs:
            lines.append(f"<details><summary>🔄 Agent 流转日志 ({len(logs)} 条)</summary>")
            lines.append("")
            for log in logs:
                ts = log.get("timestamp", "")
                node = log.get("node", "")
                status = log.get("status", "")
                msg = log.get("message", "")
                lines.append(f"- `{ts}` **{node}** [{status}] {msg}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 渲染单条历史消息
# ---------------------------------------------------------------------------

def _render_message(item: Dict[str, Any], idx: int) -> None:
    """渲染单条聊天消息（用户/助手）。"""
    role = item.get("role", "user")
    content = item.get("content", "")

    if role == "user":
        with st.chat_message("user"):
            st.markdown(content)
    else:
        with st.chat_message("assistant"):
            st.markdown(content)

            # 召回片段（可折叠）
            chunks = item.get("retrieval_chunks") or []
            if chunks:
                with st.expander(f"📎 Chroma 召回原文片段（{len(chunks)} 条）", expanded=False):
                    for i, c in enumerate(chunks, 1):
                        st.markdown(f"**片段 {i}** · score=`{c.get('score', 0):.4f}`")
                        meta = c.get("metadata") or {}
                        if meta:
                            meta_str = " · ".join(f"{k}={v}" for k, v in meta.items() if v)
                            if meta_str:
                                st.caption(meta_str)
                        st.code(c.get("content", "")[:800], language="text")
                        st.divider()

            # 流转日志（可折叠）
            logs = item.get("execution_log") or []
            if logs:
                with st.expander(f"🔄 Agent 流转日志（{len(logs)} 条）", expanded=False):
                    for log in logs:
                        ts = log.get("timestamp", "")
                        node = log.get("node", "")
                        status = log.get("status", "")
                        msg = log.get("message", "")
                        icon = "✅" if status == "ok" else ("⚠️" if status == "warn" else "❌")
                        st.markdown(f"{icon} `{ts}` **{node}** — {msg}")


# ---------------------------------------------------------------------------
# 主渲染函数
# ---------------------------------------------------------------------------

def render(backend) -> None:
    """
    渲染智能问答页。

    Args:
        backend: ApiClient 或 WorkflowBridge 实例
    """
    st.markdown("# 💬 智能问答")
    st.caption("多 Agent 协同：路由分发 → QA/检索 → 反思校验 → 最终答案")
    
    from api.config import settings
    is_cloud = settings.use_cloud_model
    model_badge = "☁️ 云端在线模型" if is_cloud else "🦙 本地离线模型"
    st.markdown(f"**当前模型：{model_badge}**")

    # ---- 顶部：温度调节 + 导出 ----
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        temperature = st.slider(
            "🌡️ LLM 温度（temperature）",
            min_value=0.0,
            max_value=1.5,
            value=float(st.session_state.get("temperature", 0.7)),
            step=0.1,
            help="0=严谨确定，1.5=发散创造。数理推理推荐 0.3，开放问答推荐 0.7。",
        )
        st.session_state.temperature = temperature
    with col2:
        st.write("")
        st.write("")
        if st.button("🗑️ 清空对话", use_container_width=True):
            st.session_state.chat_history = []
            st.rerun()
    with col3:
        st.write("")
        st.write("")
        if st.button("📥 导出MD", use_container_width=True):
            if st.session_state.chat_history:
                md = _build_markdown(st.session_state.chat_history)
                st.download_button(
                    label="下载 Markdown",
                    data=md.encode("utf-8"),
                    file_name=f"studymate_chat_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
                    mime="text/markdown",
                )
            else:
                st.warning("暂无对话")

    st.divider()

    # ---- 渲染历史对话 ----
    history = st.session_state.chat_history
    for i, item in enumerate(history):
        _render_message(item, i)

    # ---- 输入框 ----
    user_input = st.chat_input("请输入你的问题，例如：求解方程 x^2 - 5x + 6 = 0")

    if user_input:
        # 1) 先把用户消息加入历史并渲染
        history.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        # 2) 调用后端（带 loading 占位）
        with st.chat_message("assistant"):
            with st.spinner("🤔 Agent 思考中..."):
                # 把历史转换成 backend 需要的格式（仅 user/assistant 角色对）
                chat_history_for_api = [
                    {"role": h["role"], "content": h["content"]}
                    for h in history[:-1]  # 排除当前刚加的用户消息
                    if h.get("role") in ("user", "assistant")
                ]
                resp = backend.chat(
                    query=user_input,
                    history=chat_history_for_api,
                    temperature=temperature,
                )

        # 3) 处理响应
        if not isinstance(resp, dict) or not resp.get("success", False):
            show_error_popup(resp)
            # 错误时也记一条占位助手消息
            history.append({
                "role": "assistant",
                "content": "⚠️ 抱歉，处理失败，请参考上方错误提示重试。",
                "retrieval_chunks": [],
                "execution_log": [],
            })
        else:
            data = resp.get("data") or {}
            final_answer = data.get("final_answer", "") or "(空回答)"
            chunks = data.get("retrieval_chunks", []) or []
            logs = data.get("execution_log", []) or []
            intent = data.get("intent", "")
            elapsed = data.get("elapsed", 0.0)

            # 渲染助手回复
            with st.chat_message("assistant"):
                st.markdown(final_answer)
                if intent:
                    st.caption(f"🎯 意图：`{intent}` · ⏱️ 耗时 {elapsed:.2f}s")

                if chunks:
                    with st.expander(f"📎 Chroma 召回原文片段（{len(chunks)} 条）", expanded=False):
                        for i, c in enumerate(chunks, 1):
                            st.markdown(f"**片段 {i}** · score=`{c.get('score', 0):.4f}`")
                            meta = c.get("metadata") or {}
                            if meta:
                                meta_str = " · ".join(
                                    f"{k}={v}" for k, v in meta.items() if v
                                )
                                if meta_str:
                                    st.caption(meta_str)
                            st.code(c.get("content", "")[:800], language="text")
                            st.divider()

                if logs:
                    with st.expander(f"🔄 Agent 流转日志（{len(logs)} 条）", expanded=False):
                        for log in logs:
                            ts = log.get("timestamp", "")
                            node = log.get("node", "")
                            status = log.get("status", "")
                            msg = log.get("message", "")
                            icon = (
                                "✅" if status == "ok"
                                else ("⚠️" if status == "warn" else "❌")
                            )
                            st.markdown(f"{icon} `{ts}` **{node}** — {msg}")

            # 入历史
            history.append({
                "role": "assistant",
                "content": final_answer,
                "retrieval_chunks": chunks,
                "execution_log": logs,
                "intent": intent,
                "elapsed": elapsed,
            })

        # 保存回 session_state
        st.session_state.chat_history = history
