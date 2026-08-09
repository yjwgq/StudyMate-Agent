# -*- coding: utf-8 -*-
"""
RouterAgent 意图路由 Agent
===========================
职责：
1. 自动识别用户输入的意图类别；
2. 输出标准化 JSON 任务结构（不直接回答用户）；
3. 为下游 Agent 提供调度依据。

支持 6 类意图：
- simple_qa     简单知识问答
- math_exercise 数理习题（需调用沙箱）
- material_retrieve 学习资料检索
- literature_assist 文献辅助
- composite     复合场景（多意图）
- irrelevant    无关问题

依据：项目硬性规则——所有 LLM 调用遵循 langchain-ollama 标准写法。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from .base_agent import BaseAgent, safe_call


# ---------------------------------------------------------------------------
# 路由 Agent 专属提示词
# ---------------------------------------------------------------------------

ROUTER_AGENT_PROMPT = """【你的角色：意图路由 Agent】
你不直接回答用户问题，只负责分析用户输入并输出标准化 JSON 任务结构，供下游 Agent 调度使用。

【支持的意图类别】
- simple_qa        : 简单知识问答（无需计算、无需检索资料，纯概念解释）
- math_exercise    : 数理习题（包含方程、微积分、数值验算等需要符号计算的场景）
- material_retrieve: 学习资料检索（明确请求查找课件、教材、笔记等内容）
- literature_assist: 文献辅助（涉及论文、参考文献、学术创新点提取）
- composite        : 复合场景（同时包含上述两种或以上意图）
- irrelevant       : 与学习无关的问题（闲聊、政治、暴力、隐私等）

【输出格式（严格 JSON，不要任何额外说明文字）】
{{{{
  \"intent\": \"simple_qa | math_exercise | material_retrieve | literature_assist | composite | irrelevant\",
  \"sub_intents\": [\"可选，复合场景下的子意图列表\"],
  \"subject\": \"学科领域，如 math / physics / chemistry / biology / cs / literature / history / other\",
  \"keywords\": [\"用户输入中的核心关键词，最多5个\"],
  \"needs_search\": true/false,
  \"needs_calculation\": true/false,
  \"confidence\": 0.0-1.0,
  \"reasoning\": \"一句话说明判断理由\",
  \"suggested_agents\": [\"下游应调用的 Agent 名称列表，如 ['QAExerciseAgent', 'RetrieveAgent']\"]
}}}}

【判断准则】
1. 出现"求导、积分、方程、解、计算、证明、推导、代入"等词 → math_exercise；
2. 出现"查找、检索、课件、教材、笔记、资料、PPT"等词 → material_retrieve；
3. 出现"论文、参考文献、文献、创新点、引文、GB/T"等词 → literature_assist；
4. 同时出现 2 类以上 → composite，并填写 sub_intents 与 suggested_agents；
5. 出现与学习无关的词（明星八卦、政治立场、暴力等） → irrelevant；
6. confidence 反映你对意图判断的把握，低于 0.5 表示不确定。

【输入】
用户输入: {user_input}

【输出】
仅输出 JSON，不要前后任何文字、不要 markdown 代码块标记。
"""


# ---------------------------------------------------------------------------
# 路由意图枚举（便于下游引用）
# ---------------------------------------------------------------------------

INTENT_SIMPLE_QA = "simple_qa"
INTENT_MATH_EXERCISE = "math_exercise"
INTENT_MATERIAL_RETRIEVE = "material_retrieve"
INTENT_LITERATURE_ASSIST = "literature_assist"
INTENT_COMPOSITE = "composite"
INTENT_IRRELEVANT = "irrelevant"

ALL_INTENTS = {
    INTENT_SIMPLE_QA,
    INTENT_MATH_EXERCISE,
    INTENT_MATERIAL_RETRIEVE,
    INTENT_LITERATURE_ASSIST,
    INTENT_COMPOSITE,
    INTENT_IRRELEVANT,
}


# ---------------------------------------------------------------------------
# RouterAgent 主体
# ---------------------------------------------------------------------------

class RouterAgent(BaseAgent):
    """意图路由 Agent。"""

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        base_url: Optional[str] = None,
    ) -> None:
        # 路由需要稳定输出，温度设为 0.1
        super().__init__(
            model=model,
            temperature=temperature if temperature is not None else 0.1,
            base_url=base_url,
            extra_agent_prompt=ROUTER_AGENT_PROMPT,
        )

    # ------------------------------------------------------------------
    # 核心执行入口
    # ------------------------------------------------------------------
    @safe_call(default_return={
        "intent": "irrelevant",
        "sub_intents": [],
        "subject": "other",
        "keywords": [],
        "needs_search": False,
        "needs_calculation": False,
        "confidence": 0.0,
        "reasoning": "路由解析失败，返回安全默认值",
        "suggested_agents": [],
        "parse_error": True,
    })
    def run(self, user_input: str, history: Optional[list] = None) -> Dict[str, Any]:
        """
        路由用户输入。

        Args:
            user_input: 用户原始输入。
            history: 历史对话（仅参考，不强制使用）。

        Returns:
            标准化任务结构字典。
        """
        self.logger.info(f"路由输入: {user_input[:80]}{'...' if len(user_input) > 80 else ''}")

        # 构建 prompt
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            ("user", "用户输入: {user_input}\n\n请输出 JSON："),
        ])

        chain = prompt | self.llm | StrOutputParser()
        raw_output = chain.invoke({"user_input": user_input})

        self.logger.debug(f"路由原始输出: {raw_output[:300]}")

        # 解析 JSON
        task = self._parse_router_output(raw_output)
        task["raw_output"] = raw_output  # 保留原始输出便于调试

        self.logger.info(
            f"路由结果: intent={task.get('intent')} "
            f"confidence={task.get('confidence')} "
            f"agents={task.get('suggested_agents')}"
        )
        return task

    # ------------------------------------------------------------------
    # JSON 解析（容错）
    # ------------------------------------------------------------------
    def _parse_router_output(self, raw: str) -> Dict[str, Any]:
        """从 LLM 输出中提取 JSON，容错处理 markdown 代码块包裹。"""
        # 去除可能的 markdown 代码块标记
        text = raw.strip()
        if text.startswith("```"):
            # 去掉首行 ```json 或 ```
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        # 尝试直接解析
        try:
            task = json.loads(text)
            return self._validate_task(task)
        except json.JSONDecodeError:
            pass

        # 尝试用正则提取第一个 {...} 块
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                task = json.loads(match.group(0))
                return self._validate_task(task)
            except json.JSONDecodeError:
                pass

        # 解析失败，返回默认值
        self.logger.warning(f"JSON 解析失败，原始输出: {text[:200]}")
        return {
            "intent": INTENT_IRRELEVANT,
            "sub_intents": [],
            "subject": "other",
            "keywords": [],
            "needs_search": False,
            "needs_calculation": False,
            "confidence": 0.0,
            "reasoning": f"JSON 解析失败，原始输出: {text[:100]}",
            "suggested_agents": [],
            "parse_error": True,
        }

    def _validate_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """校验并补全任务字段。"""
        # intent 合法性
        intent = task.get("intent", INTENT_IRRELEVANT)
        if intent not in ALL_INTENTS:
            self.logger.warning(f"未知 intent: {intent}，回退为 irrelevant")
            intent = INTENT_IRRELEVANT
        task["intent"] = intent

        # 字段补全
        task.setdefault("sub_intents", [])
        task.setdefault("subject", "other")
        task.setdefault("keywords", [])
        task.setdefault("needs_search", False)
        task.setdefault("needs_calculation", False)
        task.setdefault("confidence", 0.5)
        task.setdefault("reasoning", "")
        task.setdefault("suggested_agents", self._suggest_agents(intent, task))
        task["parse_error"] = False
        return task

    @staticmethod
    def _suggest_agents(intent: str, task: Dict[str, Any]) -> list:
        """根据 intent 推荐下游 Agent。"""
        mapping = {
            INTENT_SIMPLE_QA: ["QAExerciseAgent"],
            INTENT_MATH_EXERCISE: ["QAExerciseAgent"],
            INTENT_MATERIAL_RETRIEVE: ["RetrieveAgent"],
            INTENT_LITERATURE_ASSIST: ["RetrieveAgent"],
            INTENT_COMPOSITE: ["QAExerciseAgent", "RetrieveAgent", "ReflectionAgent"],
            INTENT_IRRELEVANT: [],
        }
        return mapping.get(intent, [])


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------

def main() -> None:
    """RouterAgent 自测：6 类典型输入各一例。"""
    print("===== RouterAgent 自测 =====\n")
    router = RouterAgent()

    test_cases = [
        ("什么是牛顿第二定律？", "simple_qa"),
        ("求解方程 x^2 - 5x + 6 = 0", "math_exercise"),
        ("帮我找一下《高等数学》第三章的课件", "material_retrieve"),
        ("请提取这篇论文的创新点并生成 GB/T 7714 引用", "literature_assist"),
        ("计算 ∫x dx 并查找相关教材", "composite"),
        ("今天天气怎么样？", "irrelevant"),
    ]

    for user_input, expected in test_cases:
        print(f"\n[输入] {user_input}")
        print(f"[期望] {expected}")
        result = router.run(user_input)
        print(f"[实际] intent={result['intent']} confidence={result.get('confidence')}")
        print(f"        agents={result.get('suggested_agents')}")
        print(f"        reasoning={result.get('reasoning', '')[:80]}")


if __name__ == "__main__":
    main()
