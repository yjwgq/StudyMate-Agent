# -*- coding: utf-8 -*-
"""
RetrieveAgent 学习资料检索 Agent
=================================
职责：
1. 区分课件检索、文献检索双分支；
2. 调用 Chroma 向量库获取相关文档片段；
3. 自动提取论文创新点、生成 GB/T 7714 参考文献；
4. 整合向量召回片段，结构化梳理知识点。

依据：项目硬性规则第4、7、11条——向量检索基于 langchain-chroma，持久化 ./chroma_db/。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from .base_agent import BaseAgent, safe_call


# ---------------------------------------------------------------------------
# 检索 Agent 专属提示词
# ---------------------------------------------------------------------------

RETRIEVE_AGENT_PROMPT = """【你的角色：学习资料检索 Agent】
你负责从 Chroma 向量库中检索学习资料，并按场景结构化输出。

【双分支策略】
- 课件检索（material）：检索课件、教材、笔记等学习资料，输出知识点梳理；
- 文献检索（literature）：检索论文、文献，输出创新点提取 + GB/T 7714 参考文献。

【输出模板 - 课件检索】
## 📚 资料检索结果
### 1. 召回片段（按相似度排序）
> 列出 top-k 召回片段，每条包含：来源、相似度分数、内容摘要（≤150字）

### 2. 知识点结构化梳理
> 将召回内容按章节/主题归并，形成知识点树：
> - 主题1
>   - 子知识点1.1（来源：[片段编号]）
>   - 子知识点1.2（来源：[片段编号]）
> - 主题2 ...

### 3. 学习建议
> 基于检索结果给出 2-3 条针对性学习建议。

【输出模板 - 文献检索】
## 📄 文献检索结果
### 1. 召回论文列表
> 列出 top-k 召回文献，每条包含：标题、作者、年份、相似度、摘要（≤200字）

### 2. 创新点提取
> 针对每篇核心文献，用 3-5 句话提炼其创新点：
> - 文献A：创新点1 / 创新点2 / 创新点3

### 3. GB/T 7714 参考文献
> 按国标格式生成参考文献列表（顺序编码制）：
> [1] 作者. 题名[文献类型标志]. 刊名, 年, 卷(期): 页码.
> [2] ...

### 4. 研究脉络
> 用一段话梳理这些文献的研究脉络与相互关系。

【输出约束】
- 严格中文 + Markdown；
- 相似度分数保留 3 位小数；
- 若向量库无相关结果，明确告知"未检索到相关资料"并给出建议；
- 参考文献编号必须连续且与正文引用对应。
"""


# ---------------------------------------------------------------------------
# 检索分支枚举
# ---------------------------------------------------------------------------

BRANCH_MATERIAL = "material"      # 课件检索
BRANCH_LITERATURE = "literature"  # 文献检索


# ---------------------------------------------------------------------------
# RetrieveAgent 主体
# ---------------------------------------------------------------------------

class RetrieveAgent(BaseAgent):
    """学习资料检索 Agent。"""

    DEFAULT_COLLECTION = "studymate_kb"
    DEFAULT_PERSIST_DIR = "./chroma_db"

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
            temperature=temperature if temperature is not None else 0.3,
            base_url=base_url,
            extra_agent_prompt=RETRIEVE_AGENT_PROMPT,
        )

        self._vector_search = vector_search
        self._persist_directory = persist_directory or self.DEFAULT_PERSIST_DIR
        self._collection_name = collection_name or self.DEFAULT_COLLECTION

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

    # ------------------------------------------------------------------
    # 分支自动判定
    # ------------------------------------------------------------------
    @staticmethod
    def detect_branch(user_input: str) -> str:
        """根据用户输入关键词判定检索分支。"""
        literature_keywords = [
            "论文", "文献", "参考文献", "引文", "创新点", "GB/T", "学术",
            "research", "paper", "literature", "citation",
        ]
        for kw in literature_keywords:
            if kw in user_input.lower() or kw in user_input:
                return BRANCH_LITERATURE
        return BRANCH_MATERIAL

    # ------------------------------------------------------------------
    # 检索执行
    # ------------------------------------------------------------------
    @safe_call(default_return=[])
    def search_material(
        self,
        query: str,
        k: int = 5,
        filter: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """课件检索：检索课件、教材、笔记。"""
        vs = self._get_vector_search()
        if filter is None:
            filter = {"type": {"$in": ["courseware", "textbook", "notes", "knowledge"]}}
        results = vs.search(query=query, k=k, filter=filter)
        self.logger.info(f"课件检索: query='{query[:40]}' 返回 {len(results)} 条")
        return results

    @safe_call(default_return=[])
    def search_literature(
        self,
        query: str,
        k: int = 5,
        filter: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """文献检索：检索论文、文献。"""
        vs = self._get_vector_search()
        if filter is None:
            filter = {"type": {"$in": ["paper", "literature", "article"]}}
        # 即使过滤条件无命中也兜底返回无过滤结果
        results = vs.search(query=query, k=k, filter=filter)
        if not results:
            self.logger.info("文献过滤检索无结果，回退到全量检索")
            results = vs.search(query=query, k=k)
        self.logger.info(f"文献检索: query='{query[:40]}' 返回 {len(results)} 条")
        return results

    # ------------------------------------------------------------------
    # 核心执行入口
    # ------------------------------------------------------------------
    @safe_call(default_return="⚠️ 检索 Agent 执行异常，请稍后重试。")
    def run(
        self,
        user_input: str,
        branch: Optional[str] = None,
        k: int = 5,
        filter: Optional[Dict] = None,
        history: Optional[list] = None,
    ) -> str:
        """
        Args:
            user_input: 用户检索请求。
            branch: 检索分支（material/literature），None 自动判定。
            k: 召回数量。
            filter: 元数据过滤条件。
            history: 历史对话。
        """
        self.logger.info(f"检索输入: {user_input[:80]}")

        # 1) 分支判定
        if branch is None:
            branch = self.detect_branch(user_input)
        self.logger.info(f"检索分支: {branch}")

        # 2) 执行向量检索
        if branch == BRANCH_LITERATURE:
            results = self.search_literature(user_input, k=k, filter=filter)
        else:
            results = self.search_material(user_input, k=k, filter=filter)

        # 3) 构建 LLM prompt
        retrieval_text = self._format_results(results, branch)
        prompt_template = self._build_prompt(branch)

        chain = prompt_template | self.llm | StrOutputParser()
        answer = chain.invoke({
            "user_input": user_input,
            "branch": branch,
            "retrieval": retrieval_text,
            "count": len(results),
        })

        self.logger.info(f"检索输出长度: {len(answer)} 字符")
        return answer

    # ------------------------------------------------------------------
    # 辅助：结果格式化与 prompt 构建
    # ------------------------------------------------------------------
    @staticmethod
    def _format_results(results: List[Dict[str, Any]], branch: str) -> str:
        if not results:
            return "（向量库未检索到相关资料）"

        lines = []
        for i, r in enumerate(results, 1):
            content = r.get("content", "")[:500]
            score = r.get("score", 0)
            meta = r.get("metadata", {})
            lines.append(
                f"[片段{i}] 相似度={score:.3f} 元数据={meta}\n内容: {content}"
            )
        return "\n\n".join(lines)

    def _build_prompt(self, branch: str) -> ChatPromptTemplate:
        """根据分支构建不同的 prompt。"""
        branch_desc = {
            BRANCH_MATERIAL: "课件/教材/笔记检索",
            BRANCH_LITERATURE: "论文/文献检索（需提取创新点 + GB/T 7714 参考文献）",
        }.get(branch, "通用检索")

        return ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            ("user",
             "## 用户检索请求\n{user_input}\n\n"
             "## 检索分支\n{branch} - {branch_desc}\n\n"
             "## 向量检索结果（共 {count} 条）\n{retrieval}\n\n"
             "请按对应模板结构化输出。"),
        ]).partial(branch=branch, branch_desc=branch_desc)

    # ------------------------------------------------------------------
    # 独立工具：直接生成 GB/T 7714 参考文献（不调用 LLM）
    # ------------------------------------------------------------------
    @staticmethod
    def format_gbt7714_reference(meta: Dict[str, Any]) -> str:
        """
        根据元数据生成单条 GB/T 7714 参考文献。

        支持字段：authors, title, journal, year, volume, issue, pages, type
        """
        authors = meta.get("authors", "佚名")
        title = meta.get("title", "无题名")
        year = meta.get("year", "n.d.")
        doc_type = meta.get("type", "J")  # J=期刊, M=专著, D=学位论文, C=会议

        if doc_type in ("J", "journal", "paper"):
            journal = meta.get("journal", "[未注明刊名]")
            volume = meta.get("volume", "")
            issue = meta.get("issue", "")
            pages = meta.get("pages", "")
            vol_issue = f"{volume}({issue})" if volume and issue else (volume or issue)
            return f"{authors}. {title}[J]. {journal}, {year}{', ' + vol_issue if vol_issue else ''}{': ' + pages if pages else ''}."
        elif doc_type in ("M", "book"):
            publisher = meta.get("publisher", "[未注明出版社]")
            place = meta.get("place", "[S.l.]")
            return f"{authors}. {title}[M]. {place}: {publisher}, {year}."
        elif doc_type in ("D", "thesis"):
            degree = meta.get("degree", "博士")
            school = meta.get("school", "[未注明学校]")
            return f"{authors}. {title}[D]. {school}, {year}."
        else:
            return f"{authors}. {title}[{doc_type}]. {year}."


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------

def main() -> None:
    """RetrieveAgent 自测。"""
    print("===== RetrieveAgent 自测 =====\n")
    agent = RetrieveAgent()

    # 分支判定测试
    print("[1] 分支判定测试:")
    test_inputs = [
        "帮我查找《高等数学》第三章课件",
        "请检索深度学习相关论文并提取创新点",
        "找一些关于牛顿第二定律的教材笔记",
    ]
    for inp in test_inputs:
        branch = RetrieveAgent.detect_branch(inp)
        print(f"  输入: {inp[:30]}... → 分支: {branch}")

    # GB/T 7714 引用测试
    print("\n[2] GB/T 7714 参考文献生成测试:")
    sample_metas = [
        {
            "authors": "张三, 李四",
            "title": "基于深度学习的图像识别研究",
            "journal": "计算机学报",
            "year": "2023",
            "volume": "46",
            "issue": "3",
            "pages": "512-525",
            "type": "J",
        },
        {
            "authors": "王五",
            "title": "机器学习方法与应用",
            "publisher": "清华大学出版社",
            "place": "北京",
            "year": "2022",
            "type": "M",
        },
    ]
    for meta in sample_metas:
        ref = RetrieveAgent.format_gbt7714_reference(meta)
        print(f"  → {ref}")

    # 完整 run 测试（若向量库可用）
    print("\n[3] 完整 run 测试:")
    answer = agent.run("帮我查找关于勾股定理的学习资料", branch=BRANCH_MATERIAL)
    print(answer[:500] + ("..." if len(answer) > 500 else ""))


if __name__ == "__main__":
    main()
