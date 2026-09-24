# ADR-1：Agent 框架选 LangGraph

> 状态：**已实现**（M4 落地，M5 扩展 interrupt/resume）
> 实现落点：`agent/graph/`（builder / state / planner / executor / react / nodes / budget）
> 依赖版本：`langgraph 1.2.12` · `langgraph-checkpoint-postgres 3.1.2`

## 背景

Agent 需要：显式可枚举的状态、**中断后能从持久化状态继续**（进程可以死）、人工审批（HITL）能暂停与恢复、多个独立子任务能并行。这些不是"调个 API 就有的能力"，而是框架层面的支撑，选型直接决定后面能不能做出来。

## 决策

使用 **LangGraph** 作为 Agent 编排框架，状态用 `TypedDict` + 显式 reducer，checkpointer 用 `AsyncPostgresSaver`（连接池），`thread_id = conversation_id`。

## 替代方案

| 方案 | 放弃理由 |
|------|---------|
| AutoGen | 对话式编排难持久化，状态不可枚举，HITL 要自行实现 |
| CrewAI | 角色模板化，流程可控性弱，难以做精细的风险分级与中断 |
| 裸 LangChain / 自研循环 | 中断恢复与状态持久化全部自研，投入产出比低 |

## 代价与局限

1. **图定义较啰嗦**：每个 channel 都要显式选 reducer，状态更新必须返回新对象（原地改列表不触发 channel 更新——这是真实的 footgun，M4 的 E8 验收专门覆盖）。
2. **API 处于演进期**：1.x 内部仍在变（`_IncludedRouter` 之类的惰性注册在排障时会造成"路由看起来没注册"的错觉）。
3. **与"函数式"写法并存**：我们没有把 ReAct 循环建成子图，而是放在节点内的 async 函数里（见下），牺牲了一部分可视化换取了实现复杂度可控。

## 与设计文档的差异（按实际实现修正）

| 设计文档写的 | 实际做法 | 原因 |
|-------------|---------|------|
| 子图（学习解题 skill 独立演进） | **未使用子图**：ReAct 循环是节点内的 async 函数 | 中断发生在工具调用深处，用子图反而要处理跨子图的状态与中断传播；当前实现里 `interrupt()` 直接在工作流节点内调用即可 |
| `Send` API（DAG 并行扇出） | **未使用 Send**：executor 自己做"依赖就绪波次 + `asyncio.gather`"调度 | 需要并发上限、失败跳过传递依赖、重规划等策略，自己调度更容易表达（`agent/graph/executor.py`） |

## 影响

- 使**中断恢复**成为可能：M5 的 F3 验收实测 `docker kill` api 容器后，在新进程里批准审批并**从 checkpoint 续跑**成功。
- 使**长任务的执行与 HTTP 连接解耦**成为可能（ADR-10）。
- 状态可枚举这一点，让"预算 / 轮次 / 降级标记"都能作为显式字段被断言（而不是散落在闭包里）。
