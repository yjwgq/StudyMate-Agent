"""LangGraph 图编排 —— 由 M4 落地。

规划结构：
    state.py      AgentState（TypedDict + 显式 reducer，禁止原地修改）
    builder.py    图装配
    routing.py    条件路由
    nodes/        planner · react · synthesize · reflection · study
"""
