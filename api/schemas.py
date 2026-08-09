# -*- coding: utf-8 -*-
"""
api/schemas.py — Pydantic 请求/响应标准化模型
============================================
职责：
为 4 个核心接口定义统一的入参 / 出参模型，保证：
1. 请求参数类型校验（避免脏数据进入工作流/沙箱）；
2. 响应结构统一（success / data / error 三段式），便于前端解析；
3. 错误码与错误信息标准化，支持前端友好弹窗。

依据：项目硬性规则——Pydantic 数据校验，统一错误返回格式。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ===========================================================================
# 一、通用响应封装
# ===========================================================================

class ApiResponse(BaseModel):
    """
    统一响应结构。

    字段说明：
    - success: 是否成功（True/False）
    - data:    成功时的业务数据（任意结构）
    - error:   失败时的错误信息对象（含错误码、消息、细节）
    - elapsed: 接口耗时（秒，便于前端展示性能）
    """
    success: bool = Field(description="请求是否成功")
    data: Optional[Any] = Field(default=None, description="业务数据")
    error: Optional["ErrorDetail"] = Field(default=None, description="错误详情")
    elapsed: float = Field(default=0.0, description="接口耗时（秒）")


class ErrorDetail(BaseModel):
    """错误详情：标准化错误码 + 用户可见消息 + 调试细节。"""

    code: str = Field(
        description="错误码："
        "OLLAMA_OFFLINE / CHROMA_LOCK / CHROMA_FAILED / "
        "SANDBOX_ERROR / TIMEOUT / VALIDATION / INTERNAL"
    )
    message: str = Field(description="用户可见的友好错误提示（中文）")
    detail: Optional[str] = Field(default=None, description="调试用细节（堆栈/原始异常）")


# 解决 ApiResponse 前向引用
ApiResponse.model_rebuild()


# ===========================================================================
# 二、/api/chat 接口模型（多 Agent 问答）
# ===========================================================================

class ChatMessage(BaseModel):
    """单条历史对话消息。"""

    role: str = Field(description="角色：user / assistant")
    content: str = Field(description="消息内容")


class ChatRequest(BaseModel):
    """/api/chat 请求体。"""

    query: str = Field(
        description="用户问题",
        min_length=1,
        max_length=2000,
    )
    history: List[ChatMessage] = Field(
        default_factory=list,
        description="历史对话（可选），用于多轮上下文",
    )
    temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=1.5,
        description="LLM 采样温度（0 严谨，1.5 发散），默认 0.7",
    )


class RetrievalChunk(BaseModel):
    """Chroma 召回的单条原文片段。"""

    content: str = Field(description="片段正文")
    score: float = Field(description="相似度分数（越小越相似，Chroma 默认 L2）")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="元数据")


class LogEntry(BaseModel):
    """Agent 流转日志单条。"""

    node: str = Field(description="节点名 router/qa/retrieve/reflection/end")
    status: str = Field(default="ok", description="状态 ok/warn/error")
    message: str = Field(default="", description="日志消息")
    timestamp: str = Field(default="", description="时间戳")
    extra: Optional[Dict[str, Any]] = Field(default=None, description="附加字段")


class ChatData(BaseModel):
    """/api/chat 成功响应的 data 字段结构。"""

    final_answer: str = Field(description="校验后的最终答案（用户可见）")
    intent: str = Field(default="", description="路由识别的主意图")
    retrieval_chunks: List[RetrievalChunk] = Field(
        default_factory=list, description="Chroma 召回原文片段列表"
    )
    execution_log: List[Dict[str, Any]] = Field(
        default_factory=list, description="Agent 流转日志"
    )
    reflection_output: str = Field(default="", description="反思校验输出")
    elapsed: float = Field(default=0.0, description="工作流耗时（秒）")


# ===========================================================================
# 三、/api/retrieve 接口模型（向量检索）
# ===========================================================================

class RetrieveRequest(BaseModel):
    """/api/retrieve 请求体。"""

    query: str = Field(description="查询文本", min_length=1, max_length=1000)
    k: int = Field(default=5, ge=1, le=50, description="返回数量")
    filter: Optional[Dict[str, Any]] = Field(
        default=None, description="元数据过滤条件"
    )
    score_threshold: Optional[float] = Field(
        default=None,
        description="相似度分数阈值（大于该值的会被过滤）",
    )


class RetrieveData(BaseModel):
    """/api/retrieve 成功响应的 data 字段结构。"""

    query: str = Field(description="原始查询")
    total: int = Field(description="召回片段数量")
    chunks: List[RetrievalChunk] = Field(description="召回片段列表")
    collection_stats: Dict[str, Any] = Field(
        default_factory=dict, description="集合统计信息"
    )


# ===========================================================================
# 四、/api/health 接口模型（健康检测）
# ===========================================================================

class HealthData(BaseModel):
    """/api/health 成功响应的 data 字段结构。"""

    status: str = Field(description="整体状态：healthy / degraded / unhealthy")
    ollama: Dict[str, Any] = Field(description="Ollama 服务状态")
    chroma: Dict[str, Any] = Field(description="Chroma 向量库状态")
    config: Dict[str, Any] = Field(description="关键配置摘要")
    checks: List[Dict[str, Any]] = Field(
        default_factory=list, description="逐项检查明细"
    )


# ===========================================================================
# 五、知识库上传接口模型（供前端调用 /api/kb/upload）
# ===========================================================================

class KbUploadData(BaseModel):
    """/api/kb/upload 成功响应的 data 字段结构。"""

    total_files: int = Field(description="上传文件数")
    parsed_files: int = Field(description="成功解析文件数")
    total_chunks: int = Field(description="分块后的 chunk 总数")
    added_count: int = Field(description="实际入库 Chroma 的文档数")
    skipped: List[str] = Field(default_factory=list, description="跳过的文件名")
    collection_stats: Dict[str, Any] = Field(
        default_factory=dict, description="入库后集合统计"
    )


# ===========================================================================
# 自测入口
# ===========================================================================
def main() -> None:
    """schemas.py 自测：打印各模型示例。"""
    print("===== schemas.py 自测 =====\n")

    err = ErrorDetail(code="OLLAMA_OFFLINE", message="Ollama 服务未启动")
    print(f"[ErrorDetail] {err.model_dump_json()}")

    req = ChatRequest(query="求解方程 x^2 - 5x + 6 = 0", temperature=0.7)
    print(f"\n[ChatRequest] {req.model_dump_json()}")

    data = ChatData(
        final_answer="x=2 或 x=3",
        intent="math_exercise",
        retrieval_chunks=[
            RetrievalChunk(content="二次方程求根公式...", score=0.12, metadata={"source": "gsm8k"})
        ],
        execution_log=[
            {"node": "router", "status": "ok", "message": "路由完成"}
        ],
        elapsed=3.45,
    )
    print(f"\n[ChatData] {data.model_dump_json()}")

    resp = ApiResponse(success=True, data=data, elapsed=3.45)
    print(f"\n[ApiResponse] success={resp.success} elapsed={resp.elapsed}")

    print("\n✅ schemas.py 自测完成！")


if __name__ == "__main__":
    main()
