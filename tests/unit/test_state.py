"""LangGraph 状态 reducer 单元测试（M4-1 / E8）。

E8 验收口径：并行节点写 `citations` 后不丢数据。
同时锁死 §7.2 的编码规范：merge_unique 去重、merge_str 去重、sum_int 累加。
"""

from agent.graph.schemas import Citation, Step
from agent.graph.state import (
    merge_chunks,
    merge_citations,
    merge_str,
    merge_tools,
    replace,
    sum_int,
)


class TestMergeUnique:
    def test_parallel_citations_no_data_loss(self) -> None:
        """E8 核心：两个并行 step 各写 2 条引用（1 条重复）→ 4-1=3 条全保留。"""
        a = [
            Citation(n=1, chunk_id="c1", document_id="d1", title="A"),
            Citation(n=2, chunk_id="c2", document_id="d1", title="A"),
        ]
        b = [
            Citation(n=1, chunk_id="c2", document_id="d1", title="A"),  # 与 a[1] 同 chunk
            Citation(n=2, chunk_id="c3", document_id="d2", title="B"),
        ]
        merged = merge_citations(a, b)
        ids = {c.chunk_id for c in merged}
        assert ids == {"c1", "c2", "c3"}
        assert len(merged) == 3

    def test_dict_citations_merge(self) -> None:
        """dict 形态（SSE citations 事件负载）同样可合并。"""
        a = [{"n": 1, "chunk_id": "x"}, {"n": 2, "chunk_id": "y"}]
        b = [{"n": 1, "chunk_id": "x"}]
        assert len(merge_citations(a, b)) == 2

    def test_chunks_merge_by_chunk_id(self) -> None:
        a = [{"chunk_id": "c1", "score": 0.9}, {"chunk_id": "c2", "score": 0.5}]
        b = [{"chunk_id": "c1", "score": 0.8}]  # 重复 chunk：保留先到者
        merged = merge_chunks(a, b)
        assert len(merged) == 2
        assert merged[0]["score"] == 0.9

    def test_none_sides(self) -> None:
        assert merge_chunks(None, [{"chunk_id": "c"}]) == [{"chunk_id": "c"}]
        assert merge_chunks([{"chunk_id": "c"}], None) == [{"chunk_id": "c"}]
        assert merge_chunks(None, None) == []


class TestOtherReducers:
    def test_replace(self) -> None:
        old = [Step(id=1, description="a")]
        new = [Step(id=2, description="b")]
        assert replace(old, new) is new

    def test_sum_int(self) -> None:
        assert sum_int(None, 5) == 5
        assert sum_int(5, None) == 5
        assert sum_int(3, 4) == 7

    def test_merge_str_dedupes(self) -> None:
        assert merge_str(["a", "b"], ["b", "c"]) == ["a", "b", "c"]
        assert merge_str(None, ["x"]) == ["x"]

    def test_merge_tools_appends(self) -> None:
        assert merge_tools([{"t": 1}], [{"t": 2}]) == [{"t": 1}, {"t": 2}]
        assert merge_tools(None, [{"t": 1}]) == [{"t": 1}]


class TestLangGraphChannelWiring:
    def test_state_channels_have_reducers(self) -> None:
        """用 LangGraph 的类型内省验证 channel 与 reducer 绑定（防手滑漏配）。"""
        from typing import get_type_hints

        from agent.graph.state import AgentState

        hints = get_type_hints(AgentState, include_extras=True)
        citations_ann = hints["citations"]
        retrieved_ann = hints["retrieved"]
        # Annotated[_, merge_unique] —— 第一个 metadata 即 reducer
        assert citations_ann.__metadata__[0] is merge_citations
        assert retrieved_ann.__metadata__[0] is merge_chunks
        assert hints["degraded"].__metadata__[0] is merge_str
        assert hints["tokens_used"].__metadata__[0] is sum_int
        assert hints["plan"].__metadata__[0] is replace

    def test_end_to_end_graph_parallel_write(self) -> None:
        """真实 LangGraph 图：两个并行节点各返回 citations → 合并结果不丢。"""

        from langgraph.graph import END, START, StateGraph

        from agent.graph.state import AgentState

        class _Mini(AgentState):
            pass

        def node_a(state):
            return {
                "citations": [
                    Citation(n=1, chunk_id="a1", document_id="d"),
                    Citation(n=2, chunk_id="a2", document_id="d"),
                ]
            }

        def node_b(state):
            return {
                "citations": [
                    Citation(n=1, chunk_id="a2", document_id="d"),  # 重复
                    Citation(n=2, chunk_id="b1", document_id="e"),
                ]
            }

        g = StateGraph(_Mini)
        g.add_node("a", node_a)
        g.add_node("b", node_b)
        g.add_edge(START, "a")
        g.add_edge(START, "b")
        g.add_edge(["a", "b"], END)
        app = g.compile()
        final = app.invoke({"user_id": "u", "conversation_id": "c"})
        chunk_ids = {c.chunk_id for c in final["citations"]}
        assert chunk_ids == {"a1", "a2", "b1"}
