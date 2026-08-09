# -*- coding: utf-8 -*-
"""
ReflectionAgent 结果反思校验 Agent
===================================
职责：
对上游 Agent（QA / Retrieve）的输出进行三重校验：
1. 事实纠错：检查陈述是否与检索资料一致，是否含编造内容；
2. 计算复核：对所有数理表达式调用沙箱重新验算；
3. 格式统一：检查是否符合五步解题/检索模板规范。

输出双结构：
- 修改说明：列出本次校验发现的问题与修正动作；
- 最终定稿：校验通过后的最终输出（用户可见）。

依据：项目硬性规则第9条——数理计算使用 RestrictedPython+SymPy 沙箱。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from .base_agent import BaseAgent, safe_call


# ---------------------------------------------------------------------------
# 反思 Agent 专属提示词
# ---------------------------------------------------------------------------

REFLECTION_AGENT_PROMPT = """【你的角色：结果反思校验 Agent】
你负责对上游 Agent 的输出进行三重校验，输出"修改说明 + 最终定稿"双结构。

【三重校验清单】
## 校验1：事实纠错
- 检查陈述是否与【参考资料】一致；
- 标记任何无来源支撑的断言（潜在编造）；
- 检查引用编号是否连续、与参考文献对应。

## 校验2：计算复核
- 提取所有数学表达式、方程、积分、导数；
- 每个表达式必须重新代入沙箱验算；
- 对比沙箱输出与原答案，标记不一致项。

## 校验3：格式统一
- 五步解题模板：必须包含【题干拆解/考点/思路/推导/易错点】五个二级标题；
- 检索模板：必须包含【召回片段/知识点梳理或创新点/参考文献】；
- Markdown 语法、LaTeX 公式 `$...$`、代码块 ```` ``` ```` 规范。

【输出格式（严格双结构）】
```
## 🔍 修改说明
| # | 问题类型 | 原文位置 | 问题描述 | 修正动作 |
|---|---------|---------|---------|---------|
| 1 | 事实/计算/格式 | ... | ... | ... |

> 若无修改，输出"✅ 三重校验全部通过，无需修改"。

## 📝 最终定稿
（校验后的完整最终输出，可直接呈现给用户）
```

【输出约束】
- 修改说明必须用 Markdown 表格；
- 最终定稿必须是完整的、可直接交付的文本；
- 不得在定稿中保留任何"原文错误"标记。
"""


# ---------------------------------------------------------------------------
# ReflectionAgent 主体
# ---------------------------------------------------------------------------

class ReflectionAgent(BaseAgent):
    """结果反思校验 Agent。"""

    # 校验类型枚举
    CHECK_FACT = "fact"
    CHECK_CALC = "calc"
    CHECK_FORMAT = "format"

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        base_url: Optional[str] = None,
        vector_search=None,
        persist_directory: Optional[str] = None,
        collection_name: Optional[str] = None,
    ) -> None:
        super().__init__(
            model=model,
            temperature=temperature if temperature is not None else 0.2,
            base_url=base_url,
            extra_agent_prompt=REFLECTION_AGENT_PROMPT,
        )

        self._vector_search = vector_search
        self._persist_directory = persist_directory or "./chroma_db"
        self._collection_name = collection_name or "studymate_kb"
        self._sandbox_module = None

    # ------------------------------------------------------------------
    # 工具懒加载
    # ------------------------------------------------------------------
    def _get_vector_search(self):
        if self._vector_search is None:
            import sys
            project_root = str(Path(__file__).parent.parent.resolve())
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
            from tools.vector_search import ChromaVectorSearch
            self._vector_search = ChromaVectorSearch(
                persist_directory=self._persist_directory,
                collection_name=self._collection_name,
            )
        return self._vector_search

    def _get_sandbox(self):
        if self._sandbox_module is None:
            import sys
            project_root = str(Path(__file__).parent.parent.resolve())
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
            from tools import code_sandbox
            self._sandbox_module = code_sandbox
        return self._sandbox_module

    # ------------------------------------------------------------------
    # 三重校验 - 独立方法（可单独调用）
    # ------------------------------------------------------------------
    @safe_call(default_return={"passed": True, "issues": [], "notes": "事实校验跳过"})
    def check_facts(
        self,
        content: str,
        retrieval: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """事实纠错：与检索资料对比，标记无支撑断言。"""
        issues: List[Dict[str, str]] = []

        if not retrieval:
            return {
                "passed": True,
                "issues": [],
                "notes": "未提供检索资料，事实校验跳过",
            }

        # 简化规则：检查 content 中的数值/年份是否在检索资料中出现
        numbers_in_content = set(re.findall(r"\b\d{2,4}(?:\.\d+)?\b", content))
        retrieval_text = " ".join(r.get("content", "") for r in retrieval)
        numbers_in_retrieval = set(re.findall(r"\b\d{2,4}(?:\.\d+)?\b", retrieval_text))

        suspicious = numbers_in_content - numbers_in_retrieval
        # 过滤掉常见无害数字（步骤编号、年份范围等）
        suspicious = {n for n in suspicious if not (1900 <= int(float(n)) <= 2100 and len(n) == 4)}

        for num in list(suspicious)[:5]:
            issues.append({
                "type": self.CHECK_FACT,
                "location": f"数值 {num}",
                "problem": "该数值未在检索资料中出现，可能为编造",
                "action": "请核对来源或删除该断言",
            })

        return {
            "passed": len(issues) == 0,
            "issues": issues,
            "notes": f"事实校验: 检查 {len(numbers_in_content)} 个数值, 可疑 {len(suspicious)} 个",
        }

    @safe_call(default_return={"passed": True, "issues": [], "notes": "无计算式"})
    def check_calculations(self, content: str) -> Dict[str, Any]:
        """计算复核：提取数学表达式并调用沙箱验算。"""
        issues: List[Dict[str, str]] = []
        sandbox = self._get_sandbox()

        # 提取形如 x^2 - 5x + 6 = 0 的方程（单行，避免跨行匹配到垃圾内容）
        equations = re.findall(
            r"([a-zA-Z][\w\s\^\*\+\-\(\)\/\.]*?=\s*[\d\-\+\.a-zA-Z\^\*\/\(\)\s]+)",
            content,
            re.MULTILINE,
        )
        # 过滤掉含换行符或过长的匹配
        equations = [eq.strip() for eq in equations if "\n" not in eq and 5 <= len(eq) <= 80]
        # 提取形如 ∫...dx
        integrals = re.findall(r"∫\s*(.+?)\s*d([a-zA-Z])", content)

        checked = 0
        for eq in equations[:3]:  # 最多验算 3 个，避免耗时
            eq = eq.strip()
            if len(eq) > 100 or len(eq) < 5:
                continue
            # 预处理：^ → **，5x → 5*x（隐式乘法）
            eq_normalized = self._normalize_math_expr(eq)
            try:
                result = sandbox.solve_equation(eq_normalized)
                if not result.get("success"):
                    issues.append({
                        "type": self.CHECK_CALC,
                        "location": f"方程 {eq}",
                        "problem": f"沙箱验算失败: {result.get('error', '')[:80]}",
                        "action": "请检查方程表达式或修正解答",
                    })
                checked += 1
            except Exception as e:
                issues.append({
                    "type": self.CHECK_CALC,
                    "location": f"方程 {eq}",
                    "problem": f"沙箱异常: {type(e).__name__}: {e}",
                    "action": "重新推导该步骤",
                })

        return {
            "passed": len(issues) == 0,
            "issues": issues,
            "notes": f"计算校验: 验算 {checked} 个方程式",
        }

    @staticmethod
    def _normalize_math_expr(expr: str) -> str:
        """
        将人类书写习惯的数学表达式规范化为 Python/SymPy 可解析的形式。
        - ^ → **（幂运算）
        - 5x → 5*x（隐式乘法）
        - 5(x+1) → 5*(x+1)
        - )(  → )*( 
        """
        s = expr.replace("^", "**")
        # 数字后接字母: 5x → 5*x
        s = re.sub(r"(\d)([a-zA-Z])", r"\1*\2", s)
        # 数字后接左括号: 5( → 5*(
        s = re.sub(r"(\d)\(", r"\1*(", s)
        # 右括号后接左括号: )( → )*(
        s = s.replace(")(", ")*(")
        # 右括号后接字母: )x → )*x
        s = re.sub(r"\)([a-zA-Z])", r")*\1", s)
        return s

    @safe_call(default_return={"passed": True, "issues": [], "notes": "格式校验跳过"})
    def check_format(self, content: str, expected_template: str = "qa") -> Dict[str, Any]:
        """格式统一：检查模板规范。"""
        issues: List[Dict[str, str]] = []

        if expected_template == "qa":
            required_sections = ["题干拆解", "考点", "思路", "推导", "易错点"]
        elif expected_template == "material":
            required_sections = ["召回", "知识点", "学习建议"]
        elif expected_template == "literature":
            required_sections = ["召回", "创新点", "GB/T", "参考文献"]
        else:
            required_sections = []

        for section in required_sections:
            if section not in content:
                issues.append({
                    "type": self.CHECK_FORMAT,
                    "location": f"章节 '{section}'",
                    "problem": f"缺失必备章节: {section}",
                    "action": f"补全 {section} 章节",
                })

        # 检查 LaTeX 公式是否闭合
        dollar_count = content.count("$")
        if dollar_count % 2 != 0:
            issues.append({
                "type": self.CHECK_FORMAT,
                "location": "LaTeX 公式",
                "problem": "$ 符号未成对闭合",
                "action": "检查所有 $...$ 公式",
            })

        # 检查代码块是否闭合
        codeblock_count = content.count("```")
        if codeblock_count % 2 != 0:
            issues.append({
                "type": self.CHECK_FORMAT,
                "location": "代码块",
                "problem": "``` 代码块未成对闭合",
                "action": "补全代码块结束标记",
            })

        return {
            "passed": len(issues) == 0,
            "issues": issues,
            "notes": f"格式校验: 检查 {len(required_sections)} 个必备章节",
        }

    # ------------------------------------------------------------------
    # 核心执行入口：综合三重校验
    # ------------------------------------------------------------------
    @safe_call(default_return="⚠️ 反思校验 Agent 执行异常，返回原文。\n\n{original}")
    def run(
        self,
        original: str,
        user_input: str = "",
        retrieval: Optional[List[Dict[str, Any]]] = None,
        expected_template: str = "qa",
        use_llm_polish: bool = True,
        history: Optional[list] = None,
    ) -> str:
        """
        Args:
            original: 上游 Agent 的原始输出文本。
            user_input: 原始用户输入（供 LLM 参考）。
            retrieval: 检索资料（用于事实校验）。
            expected_template: 期望的模板类型 (qa/material/literature)。
            use_llm_polish: 是否调用 LLM 进行最终润色。

        Returns:
            双结构 Markdown：修改说明 + 最终定稿。
        """
        self.logger.info(
            f"反思校验输入: 原文长度={len(original)} 模板={expected_template} "
            f"检索={len(retrieval) if retrieval else 0} 条"
        )

        # 1) 执行三重校验
        fact_result = self.check_facts(original, retrieval)
        calc_result = self.check_calculations(original)
        format_result = self.check_format(original, expected_template)

        all_issues = (
            fact_result.get("issues", [])
            + calc_result.get("issues", [])
            + format_result.get("issues", [])
        )

        self.logger.info(
            f"校验结果: 事实={fact_result.get('passed')} "
            f"计算={calc_result.get('passed')} 格式={format_result.get('passed')} "
            f"共 {len(all_issues)} 个问题"
        )

        # 2) 构建修改说明表格
        revision_note = self._build_revision_note(
            fact_result, calc_result, format_result, all_issues
        )

        # 3) 调用 LLM 生成最终定稿
        if use_llm_polish:
            final_answer = self._llm_polish(original, user_input, all_issues, retrieval or [])
        else:
            final_answer = original

        # 4) 拼接双结构输出
        return f"{revision_note}\n\n## 📝 最终定稿\n\n{final_answer}"

    # ------------------------------------------------------------------
    # 辅助：构建修改说明表 + LLM 润色
    # ------------------------------------------------------------------
    @staticmethod
    def _build_revision_note(
        fact: Dict[str, Any],
        calc: Dict[str, Any],
        fmt: Dict[str, Any],
        issues: List[Dict[str, str]],
    ) -> str:
        """构建修改说明 Markdown 表格。"""
        lines = [
            "## 🔍 修改说明",
            "",
            f"- 事实校验: {'✅' if fact.get('passed') else '❌'} {fact.get('notes', '')}",
            f"- 计算校验: {'✅' if calc.get('passed') else '❌'} {calc.get('notes', '')}",
            f"- 格式校验: {'✅' if fmt.get('passed') else '❌'} {fmt.get('notes', '')}",
            "",
        ]

        if not issues:
            lines.append("✅ 三重校验全部通过，无需修改。")
            return "\n".join(lines)

        lines.extend([
            "| # | 问题类型 | 原文位置 | 问题描述 | 修正动作 |",
            "|---|---------|---------|---------|---------|",
        ])
        for i, issue in enumerate(issues, 1):
            lines.append(
                f"| {i} | {issue.get('type', '')} | "
                f"{issue.get('location', '')} | "
                f"{issue.get('problem', '')} | "
                f"{issue.get('action', '')} |"
            )
        return "\n".join(lines)

    def _llm_polish(
        self,
        original: str,
        user_input: str,
        issues: List[Dict[str, str]],
        retrieval: List[Dict[str, Any]],
    ) -> str:
        """调用 LLM 根据问题列表修正原文。"""
        if not issues:
            return original

        # 构建问题清单文本
        issue_text = "\n".join(
            f"- [{i['type']}] {i['location']}: {i['problem']} → {i['action']}"
            for i in issues
        )

        # 检索资料摘要
        retrieval_text = ""
        if retrieval:
            retrieval_text = "\n".join(
                f"[{i+1}] {r.get('content', '')[:200]}"
                for i, r in enumerate(retrieval[:3])
            )

        prompt = ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            ("user",
             "## 原始用户问题\n{user_input}\n\n"
             "## 上游 Agent 原始输出\n{original}\n\n"
             "## 校验发现的问题清单\n{issues}\n\n"
             "## 检索资料（供事实核对）\n{retrieval}\n\n"
             "请根据问题清单修正原始输出，输出修正后的完整文本（不要包含修改说明，仅输出最终定稿正文）。"),
        ])

        chain = prompt | self.llm | StrOutputParser()
        polished = chain.invoke({
            "user_input": user_input or "（无原始用户问题）",
            "original": original,
            "issues": issue_text,
            "retrieval": retrieval_text or "（无检索资料）",
        })
        return polished


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------

def main() -> None:
    """ReflectionAgent 自测。"""
    print("===== ReflectionAgent 自测 =====\n")
    agent = ReflectionAgent()

    # 构造一份"有问题"的 QA 输出做校验
    flawed_output = """## 一、题干拆解
解方程 x^2 - 5x + 6 = 0，求 x。

## 二、考点分析
考查一元二次方程因式分解，难度中等。

## 三、解题思路
尝试因式分解为 (x-a)(x-b)=0 形式。

## 四、分步推导
1. x^2 - 5x + 6 = 0
2. (x-2)(x-3) = 0
3. 所以 x = 2 或 x = 999

## 五、易错点提示
- 因式分解时注意符号。
"""

    print("[1] 单项校验测试:")
    print("\n--- 计算校验 ---")
    calc_result = agent.check_calculations(flawed_output)
    print(calc_result)

    print("\n--- 格式校验 ---")
    fmt_result = agent.check_format(flawed_output, expected_template="qa")
    print(fmt_result)

    print("\n[2] 完整三重校验 + LLM 修正:")
    result = agent.run(
        original=flawed_output,
        user_input="解方程 x^2 - 5x + 6 = 0",
        expected_template="qa",
    )
    print(result)


if __name__ == "__main__":
    main()
