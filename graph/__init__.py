# -*- coding: utf-8 -*-
"""
graph 模块：LangGraph 多 Agent 工作流编排
=========================================
导出主要接口：
- AgentState / SubTask / make_log_entry  全局状态定义
- build_workflow / get_graph / run_query 工作流构建与调用
- registry                              Agent 实例注册表（共享 Chroma）

典型用法：
    from graph import run_query
    result = run_query("求解方程 x^2 - 5x + 6 = 0")
    print(result["final_answer"])
"""
from graph.state import AgentState, SubTask, make_log_entry
from graph.workflow import (
    _AgentRegistry,
    build_workflow,
    classify_error,
    get_graph,
    registry,
    run_query,
)

__all__ = [
    "AgentState",
    "SubTask",
    "make_log_entry",
    "build_workflow",
    "get_graph",
    "run_query",
    "registry",
    "_AgentRegistry",
    "classify_error",
]

__version__ = "1.0.0"
