# -*- coding: utf-8 -*-
"""
QAExerciseAgent 学科问答 & 习题 Agent
======================================
职责：
1. 对接 tools/vector_search.ChromaVectorSearch 进行知识检索；
2. 集成 tools/code_sandbox 安全数理计算沙箱；
3. 固定五步解题输出模板：
   ① 题干拆解
   ② 考点分析
   ③ 解题思路
   ④ 分步推导（含沙箱验算）
   ⑤ 易错点提示

依据：项目硬性规则第3、4、9、11条。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from .base_agent import BaseAgent, safe_call


# ---------------------------------------------------------------------------
# 习题讲解专属提示词（内置在代码中）
# ---------------------------------------------------------------------------

QA_EXERCISE_PROMPT = """【你的角色：学科问答 & 习题讲解 Agent】
你需要以严谨、结构化的方式回答学科问题或讲解习题。

【工作流程】
1. 若用户问题需要知识背景，调用 retrieve_knowledge(query) 获取相关资料（已注入）；
2. 若涉及数理计算，必须通过 compute_in_sandbox(code) 调用安全沙箱完成验算；
3. 严格按"五步解题模板"输出最终答案。

【五步解题模板（必须完整输出）】
## 一、题干拆解
> 提取题目中的已知条件、未知量、约束关系，用列表清晰呈现。

## 二、考点分析
> 指出本题考查的核心知识点、所属章节、难度等级（基础/中等/拔高）。

## 三、解题思路
> 用 3~5 句话阐述整体解题路径，不展开推导细节。

## 四、分步推导
> 逐步骤推导，每一步必须包含：
> - 步骤编号与说明
> - 数学表达式（LaTeX 符号）
> - 关键计算结果（若涉及数理计算，标注【沙箱验算】并附 sandbox 输出）

## 五、易错点提示
> 列出 2~3 条本题常见的易错点与对应规避方法。

【输出约束】
- 严格使用中文 + Markdown；
- 数学公式用 `$...$` 或 `$$...$$` 包裹；
- 不允许跳步，不允许直接给出最终答案而无推导；
- 沙箱验算结果必须以 ```text 代码块呈现。
"""


# ---------------------------------------------------------------------------
# QAExerciseAgent 主体
# ---------------------------------------------------------------------------

class QAExerciseAgent(BaseAgent):
    """学科问答 & 习题讲解 Agent。"""

    DEFAULT_COLLECTION = "studymate_kb"
    DEFAULT_PERSIST_DIR = "./chroma_db"

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        base_url: Optional[str] = None,
        vector_search=None,
        enable_search: bool = True,
        persist_directory: Optional[str] = None,
        collection_name: Optional[str] = None,
    ) -> None:
        """
        Args:
            vector_search: 已初始化的 ChromaVectorSearch 实例（None 时按需自建）。
            enable_search: 是否启用向量检索。
            persist_directory / collection_name: 自建 vector_search 时使用。
        """
        super().__init__(
            model=model,
            temperature=temperature if temperature is not None else 0.3,
            base_url=base_url,
            extra_agent_prompt=QA_EXERCISE_PROMPT,
        )

        self.enable_search = enable_search
        self._vector_search = vector_search
        self._persist_directory = persist_directory or self.DEFAULT_PERSIST_DIR
        self._collection_name = collection_name or self.DEFAULT_COLLECTION

        # 沙箱延迟导入，避免循环依赖
        self._sandbox_module = None

    # ------------------------------------------------------------------
    # 工具初始化（懒加载）
    # ------------------------------------------------------------------
    def _get_vector_search(self):
        """懒加载 ChromaVectorSearch。"""
        if self._vector_search is None and self.enable_search:
            # 项目内绝对导入（兼容 uv run 直接运行）
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
        """懒加载 code_sandbox 模块。"""
        if self._sandbox_module is None:
            import sys
            project_root = str(Path(__file__).parent.parent.resolve())
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
            from tools import code_sandbox
            self._sandbox_module = code_sandbox
        return self._sandbox_module

    # ------------------------------------------------------------------
    # 工具调用封装（供 Agent 内部使用）
    # ------------------------------------------------------------------
    @safe_call(default_return=[])
    def retrieve_knowledge(self, query: str, k: int = 4) -> List[Dict[str, Any]]:
        """从 Chroma 向量库检索相关知识片段。"""
        vs = self._get_vector_search()
        if vs is None:
            return []
        results = vs.search(query=query, k=k)
        self.logger.info(f"向量检索: query='{query[:40]}...' 返回 {len(results)} 条")
        return results

    @safe_call(default_return={"success": False, "error": "沙箱调用失败"})
    def compute_in_sandbox(self, code: str, timeout: int = 10) -> Dict[str, Any]:
        """调用安全沙箱执行数理计算代码。"""
        sandbox = self._get_sandbox()
        result = sandbox.execute(code, timeout=timeout)
        self.logger.info(
            f"沙箱执行: success={result.get('success')} "
            f"output_len={len(result.get('output', ''))}"
        )
        return result

    # ------------------------------------------------------------------
    # 高层沙箱便捷接口
    # ------------------------------------------------------------------
    def sandbox_solve(self, equation: str, variable: str = "x") -> Dict[str, Any]:
        return self._get_sandbox().solve_equation(equation, variable)

    def sandbox_derivative(self, expr: str, variable: str = "x", order: int = 1) -> Dict[str, Any]:
        return self._get_sandbox().compute_derivative(expr, variable, order)

    def sandbox_integral(self, expr: str, variable: str = "x",
                         lower: Optional[str] = None, upper: Optional[str] = None) -> Dict[str, Any]:
        return self._get_sandbox().compute_integral(expr, variable, lower, upper)

    def sandbox_verify(self, expr: str, variable: str = "x", value: str = "1") -> Dict[str, Any]:
        return self._get_sandbox().numeric_verify(expr, variable, value)

    # ------------------------------------------------------------------
    # 核心执行入口
    # ------------------------------------------------------------------
    @safe_call(default_return="⚠️ 学科问答 Agent 执行异常，请稍后重试。")
    def run(
        self,
        user_input: str,
        history: Optional[list] = None,
        use_search: Optional[bool] = None,
        sandbox_code: Optional[str] = None,
    ) -> str:
        """
        执行学科问答 / 习题讲解。

        Args:
            user_input: 用户问题或题目。
            history: 历史对话。
            use_search: 是否启用向量检索（None 表示按 enable_search 默认）。
            sandbox_code: 预设的沙箱计算代码（可选，由路由层或上层传入）。

        Returns:
            五步解题模板的 Markdown 文本。
        """
        self.logger.info(f"QA 输入: {user_input[:80]}")

        # 1) 向量检索（按需）
        retrieval_context = ""
        search_enabled = self.enable_search if use_search is None else use_search
        if search_enabled:
            results = self.retrieve_knowledge(user_input, k=4)
            if results:
                retrieval_context = self._format_retrieval(results)
                self.logger.info(f"检索上下文长度: {len(retrieval_context)} 字符")

        # 2) 沙箱预计算（若提供）
        sandbox_context = ""
        if sandbox_code:
            sb_result = self.compute_in_sandbox(sandbox_code)
            sandbox_context = self._format_sandbox_result(sandbox_code, sb_result)
        elif self._looks_like_math(user_input):
            # 自动尝试方程求解
            sb_result = self._auto_math_compute(user_input)
            if sb_result:
                sandbox_context = sb_result

        # 3) 构建 prompt 调用 LLM
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            ("user", self._build_user_prompt(user_input, retrieval_context, sandbox_context)),
        ])

        chain = prompt | self.llm | StrOutputParser()
        answer = chain.invoke({})

        self.logger.info(f"QA 输出长度: {len(answer)} 字符")
        return answer

    # ------------------------------------------------------------------
    # 辅助：格式化与启发式
    # ------------------------------------------------------------------
    @staticmethod
    def _format_retrieval(results: List[Dict[str, Any]]) -> str:
        """格式化检索结果为上下文文本。"""
        if not results:
            return ""
        lines = ["【向量检索到的知识片段】"]
        for i, r in enumerate(results, 1):
            content = r.get("content", "")[:300]
            score = r.get("score", 0)
            meta = r.get("metadata", {})
            source = meta.get("source", "未知来源")
            lines.append(f"[{i}] (相似度={score:.3f}, 来源={source})\n{content}")
        return "\n\n".join(lines)

    @staticmethod
    def _format_sandbox_result(code: str, result: Dict[str, Any]) -> str:
        """格式化沙箱执行结果。"""
        lines = ["【沙箱验算】", f"代码:\n```python\n{code}\n```"]
        if result.get("success"):
            lines.append(f"输出:\n```text\n{result.get('output', '').strip()}\n```")
            if result.get("result") is not None:
                lines.append(f"最终值: `{result['result']}`")
        else:
            lines.append(f"❌ 失败: {result.get('error', '未知错误')}")
        return "\n\n".join(lines)

    @staticmethod
    def _looks_like_math(text: str) -> bool:
        """启发式判断是否为数理问题。"""
        patterns = [
            r"求\s*解", r"方程", r"导数", r"积分", r"微\s*分",
            r"计算", r"证\s*明", r"推\s*导", r"=\s*\d", r"\^",
            r"∫", r"∑", r"\blim\b", r"sin|cos|tan|log",
        ]
        return any(re.search(p, text) for p in patterns)

    def _auto_math_compute(self, user_input: str) -> str:
        """对明显的数学问题自动尝试沙箱求解。"""
        # 提取形如 "x^2 - 4 = 0" 的方程
        eq_match = re.search(r"([a-zA-Z][\w\s\^\*\+\-\(\)\/\.]*?=\s*[\d\-\+\.a-zA-Z\^\*\/\(\)]+)", user_input)
        if eq_match:
            equation = eq_match.group(1).strip()
            self.logger.info(f"自动检测到方程: {equation}")
            result = self.sandbox_solve(equation)
            # 注意：RestrictedPython 禁止下划线开头变量名；print 改用 output 绕开转换
            code = (
                f"# 自动求解方程: {equation}\n"
                f"eq = Eq(sympify('{equation.split('=')[0].strip()}') - sympify('{equation.split('=')[1].strip()}'), 0)\n"
                f"sol = solve(eq)\n"
                f"output('方程:', eq)\n"
                f"output('解:', sol)"
            )
            return self._format_sandbox_result(code, result)

        # 提取形如 "求 ∫x^2 dx" 的积分
        if re.search(r"∫|积分|求积", user_input):
            var_match = re.search(r"d([a-zA-Z])", user_input)
            var = var_match.group(1) if var_match else "x"
            expr_match = re.search(r"[∫∫]\s*(.+?)\s*d[a-zA-Z]", user_input)
            if expr_match:
                expr = expr_match.group(1).strip()
                self.logger.info(f"自动检测到积分: ∫{expr} d{var}")
                result = self.sandbox_integral(expr, var)
                code = (
                    f"# 自动求积分: ∫{expr} d{var}\n"
                    f"var = symbols('{var}')\n"
                    f"expr = sympify('{expr}')\n"
                    f"result = integrate(expr, var)\n"
                    f"output('不定积分:', simplify(result))"
                )
                return self._format_sandbox_result(code, result)

        return ""

    @staticmethod
    def _build_user_prompt(user_input: str, retrieval: str, sandbox: str) -> str:
        """构建发送给 LLM 的完整用户消息。"""
        user_input = user_input.replace("{", "{{").replace("}", "}}")
        retrieval = retrieval.replace("{", "{{").replace("}", "}}")
        sandbox = sandbox.replace("{", "{{").replace("}", "}}")
        
        parts = [f"## 用户问题\n{user_input}\n"]
        if retrieval:
            parts.append(f"## 参考资料\n{retrieval}\n")
        if sandbox:
            parts.append(f"## 沙箱预计算结果\n{sandbox}\n")
        parts.append("请按五步解题模板输出最终答案。")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------

def main() -> None:
    """QAExerciseAgent 自测。"""
    print("===== QAExerciseAgent 自测 =====\n")
    agent = QAExerciseAgent(enable_search=False)

    test_cases = [
        "请讲解勾股定理并给出一道例题",
        "求解方程 x^2 - 5x + 6 = 0",
        "求函数 f(x) = x^3 的导数",
    ]

    for user_input in test_cases:
        print(f"\n{'=' * 60}\n[输入] {user_input}\n{'=' * 60}")
        answer = agent.run(user_input)
        print(answer)


if __name__ == "__main__":
    main()
