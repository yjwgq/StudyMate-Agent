# -*- coding: utf-8 -*-
"""
LangGraph 全局状态定义
======================
使用 Pydantic 定义 AgentState 结构体，作为整个工作流的状态容器。

依据：项目硬性规则——禁止直接 eval，使用 Pydantic 做数据校验；
      LangGraph 1.2.9 支持 Pydantic BaseModel 作为 StateSchema。

状态字段分组：
  1) 用户输入         user_query / history
  2) 路由结果         router_result / intent / sub_intents
  3) 复合任务调度     sub_tasks / current_task_index
  4) Agent 输出       qa_outputs / retrieve_outputs / reflection_output
  5) Chroma 检索片段  retrieval_chunks
  6) 最终答案         final_answer
  7) 流转日志         execution_log
  8) 异常控制         error / should_end

reducer 策略：
  - 列表字段使用 Annotated[List[...], add] 实现"追加合并"，避免后节点覆盖前节点；
  - 标量字段使用默认覆盖语义。
"""
from __future__ import annotations

from operator import add
from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 子任务定义（复合场景拆分用）
# ---------------------------------------------------------------------------

class SubTask(BaseModel):
    """复合任务拆分后的子任务单元。"""
    task_type: str = Field(description="子任务类型: qa / retrieve / literature")
    content: str = Field(description="子任务具体内容")
    branch: Optional[str] = Field(
        default=None,
        description="检索分支 material/literature（仅 retrieve 类型用）",
    )


# ---------------------------------------------------------------------------
# 全局状态：LangGraph AgentState
# ---------------------------------------------------------------------------

class AgentState(BaseModel):
    """
    LangGraph 全局状态结构体。

    节点函数返回 dict 时，LangGraph 会按字段 reducer 合并到当前 state。
    列表字段使用 `Annotated[List[...], add]` 实现追加；其它字段覆盖。
    """

    # ----- 1) 用户输入 -----
    user_query: str = Field(default="", description="用户原始查询")
    history: List[Dict[str, str]] = Field(
        default_factory=list, description="历史对话列表"
    )

    # ----- 2) 路由结果 -----
    router_result: Dict[str, Any] = Field(
        default_factory=dict, description="RouterAgent 完整返回结果"
    )
    intent: str = Field(default="", description="主意图类别")
    sub_intents: List[str] = Field(
        default_factory=list, description="子意图列表（复合场景）"
    )

    # ----- 3) 复合任务调度 -----
    sub_tasks: List[SubTask] = Field(
        default_factory=list, description="复合任务拆分后的子任务队列"
    )
    current_task_index: int = Field(
        default=0, description="当前正在执行的子任务下标"
    )

    # ----- 4) Agent 输出（列表，便于复合任务串行累积） -----
    qa_outputs: Annotated[List[str], add] = Field(
        default_factory=list, description="QAExerciseAgent 输出列表（追加）"
    )
    retrieve_outputs: Annotated[List[str], add] = Field(
        default_factory=list, description="RetrieveAgent 输出列表（追加）"
    )

    # ----- 5) Chroma 检索片段（追加，便于反思校验复用） -----
    retrieval_chunks: Annotated[List[Dict[str, Any]], add] = Field(
        default_factory=list, description="Chroma 向量检索召回片段（追加）"
    )

    # ----- 6) 反思校验与最终答案 -----
    reflection_output: str = Field(default="", description="ReflectionAgent 输出")
    final_answer: str = Field(default="", description="校验后最终答案（用户可见）")

    # ----- 7) 流转日志（追加，记录每个节点的执行轨迹） -----
    execution_log: Annotated[List[Dict[str, Any]], add] = Field(
        default_factory=list, description="工作流节点流转日志（追加）"
    )

    # ----- 8) 异常控制 -----
    error: Optional[str] = Field(default=None, description="全局异常信息")
    should_end: bool = Field(default=False, description="是否立即终止流程")

    # ----- 9) 模型模式 -----
    model_mode: str = Field(default="local", description="模型模式: local / cloud")

    # 允许任意类型（如 Chroma 客户端对象等）
    model_config = ConfigDict(arbitrary_types_allowed=True)


# ---------------------------------------------------------------------------
# 流转日志辅助函数（节点统一调用）
# ---------------------------------------------------------------------------

def make_log_entry(
    node: str,
    status: str = "ok",
    message: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    生成标准化的流转日志条目。

    Args:
        node: 节点名称（如 "router" / "qa" / "retrieve" / "reflection" / "end"）
        status: 状态（ok / warn / error）
        message: 日志消息
        extra: 附加字段
    """
    import time
    entry: Dict[str, Any] = {
        "node": node,
        "status": status,
        "message": message,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if extra:
        entry["extra"] = extra
    return entry


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------

def main() -> None:
    """state.py 自测：验证 AgentState 默认值与追加 reducer。"""
    print("===== AgentState 自测 =====\n")

    s = AgentState(user_query="求解方程 x^2 - 5x + 6 = 0")
    print(f"[1] 初始 state: user_query={s.user_query!r}")
    print(f"    intent={s.intent!r}  should_end={s.should_end}")
    print(f"    execution_log={s.execution_log}")

    # 模拟追加日志
    s.execution_log.append(make_log_entry("router", "ok", "路由完成"))
    s.execution_log.append(make_log_entry("qa", "ok", "QA 完成"))
    print(f"\n[2] 追加日志后: {len(s.execution_log)} 条")
    for log in s.execution_log:
        print(f"    - {log['node']:>10} | {log['status']:>5} | {log['message']}")

    # 子任务示例
    s.sub_tasks = [
        SubTask(task_type="qa", content="计算 ∫x dx"),
        SubTask(task_type="retrieve", content="查找微积分教材", branch="material"),
    ]
    print(f"\n[3] 复合任务拆分: {len(s.sub_tasks)} 个子任务")
    for i, t in enumerate(s.sub_tasks):
        print(f"    [{i}] type={t.task_type} branch={t.branch} content={t.content}")

    print("\n✅ AgentState 自测完成！")


if __name__ == "__main__":
    main()
