"""Groundedness 校验器单元测试（M3-3 / D1 的量化依据）。

D1 验收口径：无脚注的事实性论断占比 < 20% —— 统计逻辑必须先在这里锁死。
"""

from agent.guardrails.groundedness import check_groundedness, split_sentences


class TestSplitSentences:
    def test_basic_cjk(self) -> None:
        assert split_sentences("第一句。第二句！第三句？") == [
            "第一句。", "第二句！", "第三句？",
        ]

    def test_newline_splits(self) -> None:
        outs = split_sentences("- 监督学习需要标注[1]\n- 无监督不需要[2]")
        assert len(outs) == 2

    def test_no_punctuation_tail(self) -> None:
        assert split_sentences("结尾没有标点") == ["结尾没有标点"]


class TestCheckGroundedness:
    def test_all_cited_passes(self) -> None:
        text = "监督学习需要标注数据才能训练模型[1]。无监督学习不需要人工标注[2]。"
        s = check_groundedness(text)
        assert s.total_claims == 2
        assert s.cited_claims == 2
        assert s.unsupported_ratio == 0.0
        assert s.ok is True

    def test_no_citation_fails(self) -> None:
        text = "监督学习需要标注数据才能训练模型。深度学习模型参数量逐年增长。"
        s = check_groundedness(text)
        assert s.total_claims == 2
        assert s.cited_claims == 0
        assert s.unsupported_ratio == 1.0
        assert s.ok is False

    def test_below_threshold_passes(self) -> None:
        # 5 句事实句，1 句无引用 = 20%，边界值（<= threshold）通过
        text = "句一[1]。句二[1]。句三[2]。句四[2]。这是一句没有引用的事实内容。"
        s = check_groundedness(text)
        assert s.total_claims == 5
        assert abs(s.unsupported_ratio - 0.2) < 1e-9
        assert s.ok is True

    def test_multi_citation_per_sentence(self) -> None:
        s = check_groundedness("对比学习结合了两者特点[1][3]。")
        assert s.cited_claims == 1

    def test_transitions_not_counted(self) -> None:
        s = check_groundedness("希望这能帮到你！以上就是全部内容。")
        assert s.total_claims == 0
        assert s.ok is True

    def test_meta_absence_not_a_claim(self) -> None:
        """「资料未提及 X」是元陈述（忠实声明），不是无出处的事实论断。

        D1 实测误判：这类句子被判无引用 → 触发无谓重写、指标虚低。
        提示词明确要求「资料不足时说明」，统计必须与提示词口径一致。
        """
        for sentence in [
            "资料未提及监督学习的目标或输出形式。",
            "资料中未列出具体的应用场景与局限。",
            "文档没有明确说明该算法的复杂度。",
            "原文不涉及评估指标的讨论。",
        ]:
            s = check_groundedness(sentence)
            assert s.total_claims == 0, sentence
            assert s.ok is True

    def test_regular_claim_with_meta_word_still_counted(self) -> None:
        """含「资料」但确实在陈述事实的句子仍须计入（不能一刀切放过）。"""
        s = check_groundedness("资料将无监督学习描述为从无标签数据中发现结构。")
        assert s.total_claims == 1
        assert s.ok is False

    def test_table_structure_rows_not_claims(self) -> None:
        """表格表头/分隔行是结构，不是事实论断（D1 实测误判）。"""
        for sentence in [
            "| 对比维度 | 监督学习 | 无监督学习 |",
            "|---|---|---|",
            "| 项目 | A | B |",
        ]:
            assert check_groundedness(sentence).total_claims == 0, sentence

    def test_table_data_row_with_content_is_claim(self) -> None:
        """数据行承载内容 → 仍计入（无引用时该被扣分）。"""
        s = check_groundedness("| 数据要求 | 监督学习需要带标签的训练数据 | 无监督不需要 |")
        assert s.total_claims == 1
        assert s.ok is False

    def test_pure_english_ignored(self) -> None:
        # MVP 面向中文语料：无 CJK 的句子不作为事实句统计
        s = check_groundedness("Supervised learning requires labels.")
        assert s.total_claims == 0

    def test_empty(self) -> None:
        s = check_groundedness("")
        assert s.total_claims == 0
        assert s.ok is True

    def test_list_items_counted(self) -> None:
        text = "- 监督学习需要标注数据[1]\n- 决策树是常见的监督算法[1]\n- 支持向量机也是"
        s = check_groundedness(text)
        assert s.total_claims == 3
        assert s.cited_claims == 2
        assert s.ok is False
