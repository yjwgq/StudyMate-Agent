"""AgentState 与 reducer（M4-1，§7.2）。

编码规范（§7.2「必须遵守」，违规是真实的 LangGraph footgun）：
    1. **禁止原地修改** state 中的列表/对象 —— LangGraph 靠节点返回的
       字典更新 channel，`state["plan"][0].status = "done"` 不会触发更新；
       必须返回新列表/新对象。
    2. 无 reducer 的标量 channel 只能由单个节点在单个超步内更新；
       多节点并发写的 channel 必须给 reducer。
    3. 新增 channel 必须显式选择 reducer 并说明理由（见各字段注释）。
"""

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages

from agent.graph.schemas import Artifact, Chunk, Citation, Step, chunks_key, citations_key

# ---------------- reducers ----------------


def replace(_old: Any, new: Any) -> Any:
    """显式覆盖语义。使用 replace 的节点必须返回新对象，禁止原地修改。"""
    return new


def _merge_unique_by(old: Any, new: Any, key: Any) -> list:
    seen: set = set()
    out: list = []
    for item in (old or []) + (new or []):
        try:
            k = key(item)
        except Exception:  # noqa: BLE001 —— key 异常时退化为整对象去重
            k = repr(item)
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


def merge_citations(old: Any, new: Any) -> list:
    """按 chunk_id 去重合并引用（并行 step 写 citations 不丢数据，E8）。

    注意：LangGraph 强制 reducer 是 (a, b) -> c 二参签名（带默认参数的
    三参函数在编译期被拒），故按 channel 拆成专用 reducer。
    """
    return _merge_unique_by(old, new, citations_key)


def merge_chunks(old: Any, new: Any) -> list:
    """按 chunk_id 去重合并检索命中（并行 react 各自检索）。"""
    return _merge_unique_by(old, new, chunks_key)


# 兼容别名：设计文档 §7.2 的 merge_unique 在本实现中拆为上面两个二参 reducer
def merge_unique(old: Any, new: Any, key: Any = None) -> list:
    """通用去重合并（仅供测试/脚本使用；channel 请用 merge_citations/merge_chunks）。"""
    return _merge_unique_by(old, new, key or chunks_key)


def sum_int(old: Any, new: Any) -> int:
    return (old or 0) + (new or 0)


def merge_str(old: Any, new: Any) -> list[str]:
    """字符串列表去重合并（degraded 原因标记，并行写不重复）。"""
    seen: set = set()
    out: list[str] = []
    for s in (old or []) + (new or []):
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def merge_tools(old: Any, new: Any) -> list[dict]:
    """工具调用埋点合并：tool_invocation 记录以 (name+序) 追加，不去重。"""
    return (old or []) + (new or [])


# ---------------- state ----------------


class AgentState(TypedDict, total=False):
    """total=False：所有键可选 —— 输入只需带增量，checkpoint 合并余下。

    reducer 选择说明（规范第 3 条）：
        messages      add_messages     LangGraph 官方消息合并（按 id 去重追加）
        retrieved     merge_chunks     并行 react 各自检索，按 chunk_id 去重（E8）
        citations     merge_citations  并行 react 各自产生引用，按 chunk_id 去重（E8）
        plan          replace          planner 是唯一写者，整表覆盖
        artifacts     replace          仅 synthesize 写
        tool_events   merge_tools      工具调用埋点流水（E6 的内存镜像，DB 才是权威）
        degraded      merge_str        降级原因去重合并（§8.3）
        tokens_used   sum_int          executor 聚合各 step 消耗（单一写者场景除外）
        其余标量       无 reducer       单写者（planner / executor / runner）
    """

    # 会话上下文
    messages: Annotated[list, add_messages]
    user_id: str
    conversation_id: str
    message_id: str              # 本轮助手消息 id（runner 预生成，工具埋点关联）
    trace_id: str
    mode: str                    # auto / chat / agent —— planner 产出

    # 计划与执行
    plan: Annotated[list[Step], replace]
    replans_left: int
    max_parallel: int

    # 检索与引用
    system_context: Annotated[str, replace]
    retrieved: Annotated[list[Chunk], merge_chunks]
    citations: Annotated[list[Citation], merge_citations]
    # citations 用 merge_unique（E8：并行 step 写引用不丢数据）。并行期各 step
    # 的编号 [n] 只是局部序号，synthesize 前由 runner 统一重编号保证全局一致。

    # 预算控制（§7.8）
    tokens_used: Annotated[int, sum_int]
    token_budget: int
    rounds_used: int
    compress_count: int

    # 工具调用流水（E6 内存镜像；DB tool_invocations 才是权威）
    tool_events: Annotated[list[dict], merge_tools]

    # HITL（M5 使用，M4 预留）
    approval_id: str | None
    pending_tool_call: dict | None

    # 降级与错误（§8.3）
    degraded: Annotated[list[str], merge_str]
    error: str | None
    artifacts: Annotated[list[Artifact], replace]

    # 最终答案（synthesize 产出；chat 分支产出）
    answer: Annotated[str, replace]
    groundedness: Annotated[dict | None, replace]
