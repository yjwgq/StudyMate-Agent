# -*- coding: utf-8 -*-
"""
frontend/views/literature_page.py — 文献整理页
=============================================
职责：
1. 调用 backend.chat 走 RetrieveAgent 的文献检索分支；
2. 展示召回文献片段、创新点提取、GB/T 7714 参考文献格式；
3. 提供独立向量检索入口（backend.retrieve），按 metadata.source 过滤；
4. 支持复制原文与导出。

依据：项目硬性规则——RetrieveAgent 区分课件/文献双分支，自动提取创新点、
      生成 GB/T 7714 参考文献。
"""
from __future__ import annotations

import streamlit as st

from frontend.error_utils import show_error_popup


def render(backend) -> None:
    """渲染文献整理页。"""
    st.markdown("# 📄 文献整理")
    st.caption("检索文献片段 · 提取创新点 · 生成 GB/T 7714 参考文献")

    # 上方 Tabs：文献辅助问答 vs 独立检索
    tab_assist, tab_search = st.tabs(["📖 文献辅助问答", "🔎 独立向量检索"])

    # ---------------- Tab 1：文献辅助问答 ----------------
    with tab_assist:
        st.markdown("### 输入文献相关问题")
        st.caption("调用 chat 接口，路由到 RetrieveAgent 文献分支（literature_assist）")

        lit_query = st.text_area(
            "描述你的文献需求",
            height=100,
            placeholder="例如：帮我整理近三年关于大模型推理能力评测的文献，提取创新点并生成 GB/T 7714 参考文献",
            key="lit_query",
        )

        col_a, col_b = st.columns([1, 3])
        with col_a:
            ask_btn = st.button(
                "🚀 文献整理",
                type="primary",
                disabled=not lit_query.strip(),
                use_container_width=True,
            )
        with col_b:
            st.caption("提示：意图关键词包含'文献/论文/综述/创新点/参考文献'等。")

        if ask_btn and lit_query.strip():
            with st.spinner("📚 RetrieveAgent 检索并整理文献中..."):
                resp = backend.chat(query=lit_query.strip(), history=[], temperature=0.5)

            if not isinstance(resp, dict) or not resp.get("success", False):
                show_error_popup(resp)
            else:
                data = resp.get("data") or {}
                answer = data.get("final_answer", "") or "(空回答)"
                elapsed = data.get("elapsed", 0.0)
                intent = data.get("intent", "")
                chunks = data.get("retrieval_chunks", []) or []
                logs = data.get("execution_log", []) or []

                st.success(f"✅ 整理完成 · ⏱️ {elapsed:.2f}s · 🎯 {intent}")

                # 文献整理结果
                st.markdown("### 📑 整理结果")
                st.markdown(answer)

                # 召回片段
                if chunks:
                    st.markdown("---")
                    st.markdown(f"### 📎 召回文献片段（{len(chunks)} 条）")
                    for i, c in enumerate(chunks, 1):
                        score = c.get("score", 0.0)
                        content = c.get("content", "")
                        meta = c.get("metadata") or {}
                        with st.expander(
                            f"片段 {i} · score={score:.4f} · {meta.get('source', '未知来源')}",
                            expanded=False,
                        ):
                            meta_str = " · ".join(
                                f"{k}={v}" for k, v in meta.items() if v
                            )
                            if meta_str:
                                st.caption(meta_str)
                            st.code(content[:1200], language="text")

                # 流转日志
                if logs:
                    with st.expander(f"🔄 Agent 流转日志（{len(logs)} 条）"):
                        for log in logs:
                            icon = "✅" if log.get("status") == "ok" else "⚠️"
                            st.markdown(
                                f"{icon} `{log.get('timestamp', '')}` "
                                f"**{log.get('node', '')}** — {log.get('message', '')}"
                            )

                # 导出按钮
                st.divider()
                export_text = f"# 文献整理结果\n\n> 查询：{lit_query}\n\n{answer}\n"
                st.download_button(
                    label="📥 导出整理结果（Markdown）",
                    data=export_text.encode("utf-8"),
                    file_name="literature_review.md",
                    mime="text/markdown",
                )

    # ---------------- Tab 2：独立向量检索 ----------------
    with tab_search:
        st.markdown("### 🔎 独立 Chroma 向量检索")
        st.caption("直接调用 retrieve 接口，可按元数据过滤")

        col_q, col_k = st.columns([3, 1])
        with col_q:
            search_query = st.text_input(
                "查询文本",
                placeholder="例如：transformer attention mechanism",
                key="lit_search_query",
            )
        with col_k:
            k_val = st.slider("k", min_value=1, max_value=30, value=8, key="lit_search_k")

        col_f1, col_f2 = st.columns(2)
        with col_f1:
            source_filter = st.text_input(
                "按 source 过滤（可选，精确匹配）",
                placeholder="留空=不过滤；例如 paper_xxx.pdf",
                key="lit_source_filter",
            )
        with col_f2:
            score_th = st.number_input(
                "score 阈值（大于该值过滤掉，0=不过滤）",
                min_value=0.0,
                max_value=2.0,
                value=0.0,
                step=0.05,
                key="lit_score_th",
            )

        if st.button("🔍 检索", disabled=not search_query.strip()):
            filter_ = None
            if source_filter.strip():
                filter_ = {"source": source_filter.strip()}
            score_threshold = score_th if score_th > 0 else None

            with st.spinner("🔎 向量检索中..."):
                resp = backend.retrieve(
                    query=search_query.strip(),
                    k=k_val,
                    filter_=filter_,
                    score_threshold=score_threshold,
                )

            if not isinstance(resp, dict) or not resp.get("success", False):
                show_error_popup(resp)
            else:
                data = resp.get("data") or {}
                chunks = data.get("chunks", []) or []
                total = data.get("total", 0)
                stats = data.get("collection_stats") or {}

                st.success(f"召回 {total} 条相关片段")
                if stats:
                    st.caption(
                        f"集合文档总数：{stats.get('count', '未知')} · "
                        f"集合名：{stats.get('name', '未知')}"
                    )

                for i, c in enumerate(chunks, 1):
                    score = c.get("score", 0.0)
                    content = c.get("content", "")
                    meta = c.get("metadata") or {}
                    with st.expander(
                        f"片段 {i} · score={score:.4f} · {meta.get('source', '未知来源')}",
                        expanded=(i <= 3),
                    ):
                        meta_str = " · ".join(
                            f"{k}={v}" for k, v in meta.items() if v
                        )
                        if meta_str:
                            st.caption(meta_str)
                        st.code(content[:1500], language="text")
