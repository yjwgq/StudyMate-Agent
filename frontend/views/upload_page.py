# -*- coding: utf-8 -*-
"""
frontend/views/upload_page.py — 知识库上传页
============================================
职责：
1. 批量上传 PDF/docx 文件；
2. 一键调用后端 upload_kb 入库 Chroma；
3. 入库后展示新增文档数、集合统计；
4. 提供检索预览（验证入库效果）。

依据：项目硬性规则——支持批量上传 PDF/docx，一键调用批量入库脚本写入 Chroma。
"""
from __future__ import annotations

from typing import List

import streamlit as st

from frontend.error_utils import show_error_popup


def render(backend) -> None:
    """
    渲染知识库上传页。

    Args:
        backend: ApiClient 或 WorkflowBridge 实例
    """
    st.markdown("# 📤 知识库上传")
    st.caption("批量上传 PDF/docx 课件与论文，自动解析→分块→写入 Chroma 向量库")

    # ---- 上传区 ----
    st.markdown("### 1️⃣ 选择文件")
    uploaded_files = st.file_uploader(
        "拖拽或点击上传 PDF / docx 文件（可多选）",
        type=["pdf", "docx"],
        accept_multiple_files=True,
        help="支持 PDF 与 Word 文档。文件将保存到 datasets/uploads/ 后入库。",
    )

    if uploaded_files:
        st.success(f"已选择 {len(uploaded_files)} 个文件：")
        for f in uploaded_files:
            size_kb = len(f.getbuffer()) / 1024 if hasattr(f, "getbuffer") else 0
            st.write(f"📄 {f.name} ({size_kb:.1f} KB)")

    st.divider()

    # ---- 入库控制 ----
    st.markdown("### 2️⃣ 一键入库")
    col_a, col_b = st.columns([1, 3])
    with col_a:
        build_btn = st.button(
            "🚀 开始入库",
            type="primary",
            disabled=not uploaded_files,
            use_container_width=True,
        )
    with col_b:
        st.caption("入库将调用 batch_build_kb，使用 lecture 分块策略（1024/128）。")

    if build_btn and uploaded_files:
        # 重置文件指针（避免重复读取后为空）
        for f in uploaded_files:
            try:
                f.seek(0)
            except Exception:
                pass

        with st.spinner("⏳ 正在解析、分块并写入 Chroma，请耐心等待..."):
            resp = backend.upload_kb(uploaded_files)

        if not isinstance(resp, dict) or not resp.get("success", False):
            show_error_popup(resp)
            return

        data = resp.get("data") or {}
        st.session_state.last_upload_result = data

        st.success(
            f"✅ 入库完成！"
            f"共 {data.get('total_files', 0)} 个文件，"
            f"成功解析 {data.get('parsed_files', 0)} 个，"
            f"新增 {data.get('added_count', 0)} 条文档。"
        )

        if data.get("skipped"):
            st.warning(f"跳过 {len(data['skipped'])} 个不支持的文件：{', '.join(data['skipped'])}")

        # 集合统计
        stats = data.get("collection_stats") or {}
        if stats:
            with st.expander("📊 集合统计", expanded=True):
                st.json(stats)

    st.divider()

    # ---- 检索预览 ----
    st.markdown("### 3️⃣ 检索预览（验证入库效果）")
    preview_query = st.text_input(
        "输入查询文本，验证刚入库的内容能否被召回",
        value="",
        placeholder="例如：微积分基本定理",
    )
    preview_k = st.slider("召回数量 k", min_value=1, max_value=20, value=5)

    if st.button("🔍 检索预览", disabled=not preview_query.strip()):
        with st.spinner("🔎 正在向量检索..."):
            resp = backend.retrieve(query=preview_query, k=preview_k)

        if not isinstance(resp, dict) or not resp.get("success", False):
            show_error_popup(resp)
            return

        data = resp.get("data") or {}
        chunks = data.get("chunks", []) or []
        total = data.get("total", 0)
        stats = data.get("collection_stats") or {}

        st.success(f"召回 {total} 条相关片段")
        if stats:
            st.caption(f"集合文档总数：{stats.get('count', '未知')}")

        for i, c in enumerate(chunks, 1):
            score = c.get("score", 0.0)
            content = c.get("content", "")
            meta = c.get("metadata") or {}
            with st.expander(
                f"片段 {i} · score={score:.4f} · {meta.get('source', '未知来源')}",
                expanded=(i == 1),
            ):
                meta_str = " · ".join(f"{k}={v}" for k, v in meta.items() if v)
                if meta_str:
                    st.caption(meta_str)
                st.code(content[:1000], language="text")
