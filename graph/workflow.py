# -*- coding: utf-8 -*-
"""
LangGraph 工作流核心
====================
基于 LangGraph 1.2.9 编排 4 大 Agent 的完整闭环调度。

【交付内容】
1. 4 个 Agent 实例化（共享 ChromaVectorSearch，避免重复加载 542MB 索引）
2. 全部节点：路由 / 复合拆分 / 问答 / 检索 / 任务推进 / 反思校验 / 结束 / 错误处理
3. 条件分支：复合任务串行执行、无关问题直接终止
4. 完整流转链路：用户输入 → 路由 → 业务 Agent → 反思校验 → 输出
5. 全局异常捕获：Ollama 离线 / Chroma 失败 / 文件锁冲突 / 沙箱报错
6. Mermaid 流程图（见文件头部注释）

【Mermaid 流程图】
```mermaid
flowchart TD
    START([用户输入]) --> Router[路由节点 RouterAgent]
    Router --> |simple_qa / math_exercise| QA[问答节点 QAExerciseAgent]
    Router --> |material_retrieve / literature_assist| Retrieve[检索节点 RetrieveAgent]
    Router --> |composite| CompositeSplit[复合任务拆分]
    Router --> |irrelevant / error| End[结束节点]
    CompositeSplit --> |首任务为 qa| QA
    CompositeSplit --> |首任务为 retrieve| Retrieve
    QA --> TaskAdvance[任务推进节点]
    Retrieve --> TaskAdvance
    TaskAdvance --> |仍有子任务-qa| QA
    TaskAdvance --> |仍有子任务-retrieve| Retrieve
    TaskAdvance --> |全部完成| Reflection[反思校验 ReflectionAgent]
    Reflection --> End
    End --> END([输出最终答案])
    Router -.-> |Ollama离线/Chroma失败/沙箱报错| End
    QA -.-> |异常| End
    Retrieve -.-> |异常| End
    Reflection -.-> |异常| End
```

依据：项目硬性规则——LLM 通过 Ollama 调用，向量检索基于 langchain-chroma，
      数理计算走 RestrictedPython+SymPy 沙箱。
"""
from __future__ import annotations

import functools
import sys
import time
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# 项目根路径注入（保证 agents/tools 模块可导入）
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from langgraph.graph import END, START, StateGraph  # noqa: E402

from agents.base_agent import logger, is_cloud_mode  # noqa: E402
from agents.qa_exercise_agent import QAExerciseAgent  # noqa: E402
from agents.reflection_agent import ReflectionAgent  # noqa: E402
from agents.retrieve_agent import (  # noqa: E402
    BRANCH_LITERATURE,
    BRANCH_MATERIAL,
    RetrieveAgent,
)
from agents.router_agent import (  # noqa: E402
    INTENT_COMPOSITE,
    INTENT_IRRELEVANT,
    INTENT_LITERATURE_ASSIST,
    INTENT_MATH_EXERCISE,
    INTENT_MATERIAL_RETRIEVE,
    INTENT_SIMPLE_QA,
    RouterAgent,
)
from api.config import settings  # noqa: E402
from graph.state import AgentState, SubTask, make_log_entry  # noqa: E402
from tools.vector_search import ChromaVectorSearch  # noqa: E402


# ===========================================================================
# 一、全局 Agent 实例（懒加载单例，共享 Chroma 客户端）
# ===========================================================================

class _AgentRegistry:
    """
    Agent 实例注册表（单例）。

    设计要点：
    - 所有 Agent 共享同一个 ChromaVectorSearch 实例，避免每个 Agent 重复加载
      542MB 的 HNSW 索引导致 Windows 内存不足；
    - 懒加载，首次访问时才初始化，避免 import 时即连接 Ollama/Chroma；
    - 提供 reset() 方法便于测试时重置。
    """

    def __init__(self) -> None:
        self._vector_search: Optional[ChromaVectorSearch] = None
        self._router: Optional[RouterAgent] = None
        self._qa: Optional[QAExerciseAgent] = None
        self._retrieve: Optional[RetrieveAgent] = None
        self._reflection: Optional[ReflectionAgent] = None

    # -- 共享 Chroma 客户端 --
    @property
    def vector_search(self) -> ChromaVectorSearch:
        if self._vector_search is None:
            logger.info("初始化共享 ChromaVectorSearch 实例")
            self._vector_search = ChromaVectorSearch()
        return self._vector_search

    # -- 4 个 Agent --
    @property
    def router(self) -> RouterAgent:
        if self._router is None:
            self._router = RouterAgent()
        return self._router

    @property
    def qa(self) -> QAExerciseAgent:
        if self._qa is None:
            self._qa = QAExerciseAgent(
                vector_search=self.vector_search,
                enable_search=True,
            )
        return self._qa

    @property
    def retrieve(self) -> RetrieveAgent:
        if self._retrieve is None:
            self._retrieve = RetrieveAgent(vector_search=self.vector_search)
        return self._retrieve

    @property
    def reflection(self) -> ReflectionAgent:
        if self._reflection is None:
            self._reflection = ReflectionAgent(vector_search=self.vector_search)
        return self._reflection

    def reset(self) -> None:
        """重置所有实例（测试用）。"""
        self._vector_search = None
        self._router = None
        self._qa = None
        self._retrieve = None
        self._reflection = None


# 全局注册表单例
registry = _AgentRegistry()


# ===========================================================================
# 二、异常分类与友好提示
# ===========================================================================

def classify_error(exc: Exception) -> Dict[str, str]:
    """
    对异常进行分类，返回 (error_type, friendly_message)。

    覆盖项目硬性规则要求的全局异常分支：
    - Ollama 离线 / 云端API失败
    - Chroma 数据库读取失败
    - 文件锁冲突
    - 代码沙箱报错
    """
    exc_name = type(exc).__name__
    exc_msg = str(exc).lower()

    # 1) 云端 API 失败（密钥错误、超时、权限不足）
    if is_cloud_mode():
        if "api_key" in exc_msg or "auth" in exc_msg or "unauthorized" in exc_msg:
            return {
                "error_type": "cloud_auth",
                "message": "⚠️ 云端API认证失败。请检查 .env 中 CLOUD_API_KEY 是否正确配置，"
                           "或切换到本地 Ollama 模式（设置 USE_CLOUD_MODEL=false）。",
            }
        if "timeout" in exc_msg or "connection" in exc_msg or "refused" in exc_msg:
            return {
                "error_type": "cloud_timeout",
                "message": "⚠️ 云端API请求超时或网络连接失败。请检查网络连接，"
                           "或切换到本地 Ollama 模式（设置 USE_CLOUD_MODEL=false）。",
            }
        if "quota" in exc_msg or "limit" in exc_msg or "exceeded" in exc_msg:
            return {
                "error_type": "cloud_quota",
                "message": "⚠️ 云端API额度耗尽或调用频率超限。请等待额度恢复，"
                           "或切换到本地 Ollama 模式（设置 USE_CLOUD_MODEL=false）。",
            }

    # 2) Ollama 离线 / 连接失败（本地模式）
    if any(k in exc_name for k in ("ConnectError", "ConnectionError", "Timeout")) \
            or "connection" in exc_msg or "refused" in exc_msg \
            or "ollama" in exc_msg:
        return {
            "error_type": "ollama_offline",
            "message": "⚠️ Ollama 服务未启动或无法连接。请运行 `ollama serve` 并确认 "
                       "http://localhost:11434 可访问，模型 qwen3:4b 已 `ollama pull`。",
        }

    # 3) 文件锁冲突（Chroma SQLite/HNSW 锁）
    if "lock" in exc_msg or "access denied" in exc_msg or "islocked" in exc_msg:
        return {
            "error_type": "file_lock",
            "message": "⚠️ 向量数据库文件锁冲突。请关闭其他占用 chroma_db 的进程，"
                       "或删除 chroma_db 目录下的 .lock 文件后重试。",
        }

    # 4) Chroma 数据库读取失败
    if "chroma" in exc_msg or "collection" in exc_msg or "sqlite" in exc_msg \
            or "hnsw" in exc_msg or "index" in exc_msg:
        return {
            "error_type": "chroma_failure",
            "message": "⚠️ Chroma 向量库读取失败。可能原因：索引损坏 / 内存不足 / "
                       "持久化目录权限问题。建议清空 ./chroma_db 后重新构建。",
        }

    # 5) 代码沙箱报错
    if "sandbox" in exc_msg or "restrictedpython" in exc_msg \
            or "syntax" in exc_msg or "sympy" in exc_msg:
        return {
            "error_type": "sandbox_error",
            "message": "⚠️ 数理计算沙箱执行报错。请检查数学表达式是否规范，"
                       "或简化表达式后重试。",
        }

    # 6) 兜底
    return {
        "error_type": "unknown",
        "message": f"⚠️ 工作流执行异常: {type(exc).__name__}: {exc}",
    }


def _check_ollama_online() -> None:
    """探测 Ollama 服务是否在线，离线则抛 ConnectionError。"""
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://localhost:11434/api/tags", method="GET"
        )
        with urllib.request.urlopen(req, timeout=3):
            return
    except Exception as e:
        raise ConnectionError(
            f"Ollama 服务不可达 (http://localhost:11434): {e}"
        )


# ===========================================================================
# 三、节点定义
# ===========================================================================

# ---------------------------------------------------------------------------
# 节点 / 路由函数 state 适配装饰器
# ---------------------------------------------------------------------------

def _normalize_state(func):
    """
    装饰器：将传入的 AgentState（Pydantic BaseModel 实例）统一转为 dict。

    原因：LangGraph 使用 Pydantic BaseModel 作为 StateSchema 时，
    节点函数接收的是 model 实例而非 dict，无法使用 state.get() / state[] 风格访问。
    本装饰器统一转为 dict，节点内部代码可保持 dict 风格不变。

    返回值仍由 LangGraph 按 reducer 合并回 state。
    """
    @functools.wraps(func)
    def wrapper(state, *args, **kwargs):
        if hasattr(state, "model_dump"):
            state = state.model_dump()
        elif hasattr(state, "dict"):
            # 兼容 Pydantic V1
            state = state.dict()
        return func(state, *args, **kwargs)
    return wrapper


def _node_with_timeout(timeout_sec: int, node_name: str):
    """
    装饰器：为节点函数添加超时保护。

    超过指定时间后强制返回错误状态，避免单个节点卡住导致整个工作流超时。

    Args:
        timeout_sec: 超时秒数
        node_name: 节点名称（用于日志和错误消息）
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(state, *args, **kwargs):
            result_container = [None]
            exception_container = [None]
            event = threading.Event()

            def target():
                try:
                    result_container[0] = func(state, *args, **kwargs)
                except Exception as e:
                    exception_container[0] = e
                finally:
                    event.set()

            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            thread.join(timeout=timeout_sec)

            if event.is_set():
                if exception_container[0] is not None:
                    raise exception_container[0]
                return result_container[0]
            else:
                logger.error(f"[{node_name}] 执行超时 ({timeout_sec}s)")
                return {
                    "error": f"⚠️ {node_name} 执行超时 ({timeout_sec}s)，请简化问题或稍后重试。",
                    "should_end": True,
                    "execution_log": [make_log_entry(
                        node_name, "error",
                        f"节点执行超时 ({timeout_sec}s)",
                        {"timeout_sec": timeout_sec},
                    )],
                }
        return wrapper
    return decorator


def _task_get(task: Any, field: str, default: Any = None) -> Any:
    """
    从 SubTask 实例或 dict 中安全获取字段。

    原因：state 经 model_dump() 后，sub_tasks 列表中的 SubTask 实例
    会被转为 dict，因此需要兼容两种形态的访问。
    """
    if hasattr(task, field):
        return getattr(task, field)
    if isinstance(task, dict):
        return task.get(field, default)
    return default


# ---------------------------------------------------------------------------
# 节点 1：路由节点
# ---------------------------------------------------------------------------

@_normalize_state
@_node_with_timeout(settings.router_timeout, "路由节点")
def router_node(state: AgentState) -> Dict[str, Any]:
    """
    路由节点：调用 RouterAgent 识别用户意图。

    异常分支：
    - 本地模式：Ollama 离线 → 设置 error，跳转 end
    - 云端模式：API 认证失败/超时 → 设置 error，跳转 end
    - LLM 返回非 JSON → RouterAgent 内部已容错，返回 irrelevant
    """
    user_query = state.get("user_query", "")
    model_mode = state.get("model_mode", "local")
    logger.info(f"[router_node] 路由输入 ({model_mode}模式): {user_query[:80]}")

    try:
        if not is_cloud_mode():
            _check_ollama_online()

        result = registry.router.run(user_query, history=state.get("history", []))
        intent = result.get("intent", INTENT_IRRELEVANT)

        logger.info(f"[router_node] 路由完成 ({model_mode}模式): intent={intent} "
                    f"confidence={result.get('confidence')}")

        return {
            "router_result": result,
            "intent": intent,
            "sub_intents": result.get("sub_intents", []),
            "execution_log": [make_log_entry(
                "router", "ok",
                f"意图={intent}, 置信度={result.get('confidence')}",
                {"reasoning": result.get("reasoning", "")[:100], "model_mode": model_mode},
            )],
        }
    except Exception as e:
        err = classify_error(e)
        logger.error(f"[router_node] 异常 ({model_mode}模式): {err['error_type']}: {e}")
        return {
            "error": err["message"],
            "should_end": True,
            "execution_log": [make_log_entry(
                "router", "error", err["message"],
                {"error_type": err["error_type"], "model_mode": model_mode},
            )],
        }


# ---------------------------------------------------------------------------
# 节点 2：复合任务拆分节点
# ---------------------------------------------------------------------------

@_normalize_state
def composite_split_node(state: AgentState) -> Dict[str, Any]:
    """
    复合任务拆分：根据 router_result.sub_intents / suggested_agents
    将复合请求拆分为多个子任务，放入 sub_tasks 队列。

    拆分规则：
    - needs_calculation=True → 增加 qa 子任务
    - needs_search=True → 增加 retrieve 子任务
    - sub_intents 包含 literature_assist → 增加 literature 分支检索子任务
    """
    router_result = state.get("router_result", {})
    user_query = state.get("user_query", "")
    sub_intents = router_result.get("sub_intents", []) or []
    suggested = router_result.get("suggested_agents", []) or []

    logger.info(f"[composite_split] 拆分复合任务: sub_intents={sub_intents}")

    sub_tasks: list[SubTask] = []

    # 1) 数理计算子任务
    if router_result.get("needs_calculation") or \
            INTENT_MATH_EXERCISE in sub_intents or \
            "QAExerciseAgent" in suggested:
        sub_tasks.append(SubTask(
            task_type="qa",
            content=user_query,
        ))

    # 2) 文献检索子任务
    if INTENT_LITERATURE_ASSIST in sub_intents or \
            any(kw in user_query for kw in ["论文", "文献", "创新点", "GB/T"]):
        sub_tasks.append(SubTask(
            task_type="retrieve",
            content=user_query,
            branch=BRANCH_LITERATURE,
        ))

    # 3) 课件检索子任务
    if INTENT_MATERIAL_RETRIEVE in sub_intents or \
            router_result.get("needs_search") or \
            "RetrieveAgent" in suggested:
        sub_tasks.append(SubTask(
            task_type="retrieve",
            content=user_query,
            branch=BRANCH_MATERIAL,
        ))

    # 兜底：至少有一个子任务
    if not sub_tasks:
        sub_tasks.append(SubTask(task_type="qa", content=user_query))

    logger.info(f"[composite_split] 拆分完成: {len(sub_tasks)} 个子任务")

    return {
        "sub_tasks": sub_tasks,
        "current_task_index": 0,
        "execution_log": [make_log_entry(
            "composite_split", "ok",
            f"拆分为 {len(sub_tasks)} 个子任务",
            {"task_types": [_task_get(t, "task_type") for t in sub_tasks]},
        )],
    }


# ---------------------------------------------------------------------------
# 节点 3：问答执行节点
# ---------------------------------------------------------------------------

@_normalize_state
@_node_with_timeout(settings.qa_timeout, "QA节点")
def qa_node(state: AgentState) -> Dict[str, Any]:
    """
    问答执行节点：调用 QAExerciseAgent。

    - 单任务场景：直接处理 user_query
    - 复合场景：处理当前 sub_tasks[idx].content
    """
    user_query = state.get("user_query", "")
    sub_tasks = state.get("sub_tasks", []) or []
    idx = state.get("current_task_index", 0)
    model_mode = state.get("model_mode", "local")

    is_composite = len(sub_tasks) > 0
    query_for_qa = _task_get(sub_tasks[idx], "content") if is_composite else user_query

    logger.info(f"[qa_node] 执行 QA ({model_mode}模式, composite={is_composite}, idx={idx}): "
                f"{query_for_qa[:60]}")

    try:
        answer = registry.qa.run(
            user_input=query_for_qa,
            history=state.get("history", []),
            use_search=True,
        )

        retrieval_chunks = _extract_qa_retrieval(answer)

        return {
            "qa_outputs": [answer],
            "retrieval_chunks": retrieval_chunks,
            "execution_log": [make_log_entry(
                "qa", "ok",
                f"QA 输出 {len(answer)} 字符",
                {"query": query_for_qa[:80], "model_mode": model_mode},
            )],
        }
    except Exception as e:
        err = classify_error(e)
        logger.error(f"[qa_node] 异常 ({model_mode}模式): {err['error_type']}: {e}")
        return {
            "error": err["message"],
            "should_end": True,
            "execution_log": [make_log_entry(
                "qa", "error", err["message"],
                {"error_type": err["error_type"], "model_mode": model_mode},
            )],
        }


def _extract_qa_retrieval(answer: str) -> list[Dict[str, Any]]:
    """
    从 QA 输出中提取检索片段元信息（用于反思校验事实核对）。
    简化实现：返回空列表，由 retrieve_node 负责真正填充。
    """
    return []


# ---------------------------------------------------------------------------
# 节点 4：检索执行节点
# ---------------------------------------------------------------------------

@_normalize_state
@_node_with_timeout(settings.retrieve_node_timeout, "检索节点")
def retrieve_node(state: AgentState) -> Dict[str, Any]:
    """
    检索执行节点：调用 RetrieveAgent。

    - 单任务场景：根据用户输入自动判定分支
    - 复合场景：使用 sub_tasks[idx].branch 指定的分支
    """
    user_query = state.get("user_query", "")
    sub_tasks = state.get("sub_tasks", []) or []
    idx = state.get("current_task_index", 0)
    model_mode = state.get("model_mode", "local")

    is_composite = len(sub_tasks) > 0
    query_for_retrieve = _task_get(sub_tasks[idx], "content") if is_composite else user_query
    branch = _task_get(sub_tasks[idx], "branch") if is_composite else None

    logger.info(f"[retrieve_node] 执行检索 ({model_mode}模式, composite={is_composite}, "
                f"branch={branch}): {query_for_retrieve[:60]}")

    try:
        vs = registry.vector_search
        k = 5
        raw_results = vs.search(query=query_for_retrieve, k=k)

        answer = registry.retrieve.run(
            user_input=query_for_retrieve,
            branch=branch,
            k=k,
        )

        return {
            "retrieve_outputs": [answer],
            "retrieval_chunks": raw_results,
            "execution_log": [make_log_entry(
                "retrieve", "ok",
                f"检索输出 {len(answer)} 字符, 召回 {len(raw_results)} 片段",
                {"branch": branch or "auto", "query": query_for_retrieve[:80], "model_mode": model_mode},
            )],
        }
    except Exception as e:
        err = classify_error(e)
        logger.error(f"[retrieve_node] 异常 ({model_mode}模式): {err['error_type']}: {e}")
        return {
            "error": err["message"],
            "should_end": True,
            "execution_log": [make_log_entry(
                "retrieve", "error", err["message"],
                {"error_type": err["error_type"], "model_mode": model_mode},
            )],
        }


# ---------------------------------------------------------------------------
# 节点 5：任务推进节点（单/复合通用）
# ---------------------------------------------------------------------------

@_normalize_state
def task_advance_node(state: AgentState) -> Dict[str, Any]:
    """
    任务推进：qa/retrieve 执行后进入此节点。

    - 单任务场景：无 sub_tasks → 直接进入 reflection
    - 复合场景：current_task_index += 1，判断是否还有子任务
    """
    sub_tasks = state.get("sub_tasks", []) or []
    idx = state.get("current_task_index", 0)

    if not sub_tasks:
        # 单任务，直接放行到 reflection
        logger.info("[task_advance] 单任务完成，进入反思校验")
        return {
            "execution_log": [make_log_entry(
                "task_advance", "ok", "单任务完成 → reflection",
            )],
        }

    next_idx = idx + 1
    if next_idx >= len(sub_tasks):
        # 全部子任务完成
        logger.info(f"[task_advance] 全部 {len(sub_tasks)} 个子任务完成 → reflection")
        return {
            "current_task_index": next_idx,
            "execution_log": [make_log_entry(
                "task_advance", "ok",
                f"全部 {len(sub_tasks)} 子任务完成 → reflection",
            )],
        }

    # 还有子任务，推进索引
    logger.info(f"[task_advance] 推进到子任务 {next_idx + 1}/{len(sub_tasks)}")
    return {
        "current_task_index": next_idx,
        "execution_log": [make_log_entry(
            "task_advance", "ok",
            f"推进到子任务 {next_idx + 1}/{len(sub_tasks)}",
            {"next_type": _task_get(sub_tasks[next_idx], "task_type")},
        )],
    }


# ---------------------------------------------------------------------------
# 节点 6：反思校验节点
# ---------------------------------------------------------------------------

@_normalize_state
@_node_with_timeout(settings.reflection_timeout, "反思校验节点")
def reflection_node(state: AgentState) -> Dict[str, Any]:
    """
    反思校验：对上游所有 Agent 输出做三重校验。

    策略：
    - 优先校验 QA 输出（expected_template=qa）
    - 若无 QA 输出，校验检索输出（material/literature）
    - 复合场景：合并所有输出后校验
    """
    qa_outputs = state.get("qa_outputs", []) or []
    retrieve_outputs = state.get("retrieve_outputs", []) or []
    retrieval_chunks = state.get("retrieval_chunks", []) or []
    user_query = state.get("user_query", "")
    model_mode = state.get("model_mode", "local")

    logger.info(f"[reflection_node] 校验输入 ({model_mode}模式): qa={len(qa_outputs)} "
                f"retrieve={len(retrieve_outputs)} chunks={len(retrieval_chunks)}")

    try:
        if qa_outputs:
            original = "\n\n---\n\n".join(qa_outputs)
            expected_template = "qa"
        elif retrieve_outputs:
            original = "\n\n---\n\n".join(retrieve_outputs)
            router_result = state.get("router_result", {})
            intent = router_result.get("intent", "")
            if intent == INTENT_LITERATURE_ASSIST or \
                    any("literature" in str(_task_get(s, "branch", "")) for s in state.get("sub_tasks", [])):
                expected_template = "literature"
            else:
                expected_template = "material"
        else:
            logger.info(f"[reflection_node] ({model_mode}模式) 无业务输出，跳过反思")
            return {
                "execution_log": [make_log_entry(
                    "reflection", "ok", "无业务输出，跳过反思",
                    {"model_mode": model_mode},
                )],
            }

        result = registry.reflection.run(
            original=original,
            user_input=user_query,
            retrieval=retrieval_chunks,
            expected_template=expected_template,
            use_llm_polish=False,
        )

        logger.info(f"[reflection_node] ({model_mode}模式) 校验输出 {len(result)} 字符")

        return {
            "reflection_output": result,
            "execution_log": [make_log_entry(
                "reflection", "ok",
                f"三重校验完成，输出 {len(result)} 字符",
                {"expected_template": expected_template, "model_mode": model_mode},
            )],
        }
    except Exception as e:
        err = classify_error(e)
        logger.error(f"[reflection_node] 异常 ({model_mode}模式): {err['error_type']}: {e}")
        fallback = (qa_outputs + retrieve_outputs)[0] if (qa_outputs or retrieve_outputs) else ""
        return {
            "reflection_output": f"⚠️ 反思校验异常，使用原文输出。\n\n{fallback}",
            "execution_log": [make_log_entry(
                "reflection", "warn", f"反思异常降级: {err['message']}",
                {"error_type": err["error_type"], "model_mode": model_mode},
            )],
        }


# ---------------------------------------------------------------------------
# 节点 7：结束节点
# ---------------------------------------------------------------------------

@_normalize_state
def end_node(state: AgentState) -> Dict[str, Any]:
    """
    结束节点：整理最终答案。

    优先级：
    1. error 非空 → 友好错误提示
    2. reflection_output 非空 → 反思校验后的最终定稿
    3. qa_outputs / retrieve_outputs → 业务原文
    4. irrelevant → 礼貌拒答
    """
    error = state.get("error")
    reflection_output = state.get("reflection_output", "")
    qa_outputs = state.get("qa_outputs", []) or []
    retrieve_outputs = state.get("retrieve_outputs", []) or []
    intent = state.get("intent", "")
    model_mode = state.get("model_mode", "local")

    if error:
        final = f"# ❌ 流程异常\n\n{error}\n\n---\n请根据提示排查后重试。"
    elif reflection_output:
        final = reflection_output
    elif qa_outputs:
        final = "\n\n---\n\n".join(qa_outputs)
    elif retrieve_outputs:
        final = "\n\n---\n\n".join(retrieve_outputs)
    elif intent == INTENT_IRRELEVANT:
        final = ("🚫 抱歉，我是 StudyMate 学习助手，只能回答与学科学习、习题讲解、"
                 "资料检索、文献辅助相关的问题。请提出一个学习类问题。")
    else:
        final = "⚠️ 未能生成有效回答，请重试或换个问法。"

    logger.info(f"[end_node] ({model_mode}模式) 最终答案长度: {len(final)} 字符")

    return {
        "final_answer": final,
        "should_end": True,
        "execution_log": [make_log_entry(
            "end", "ok", f"输出最终答案 {len(final)} 字符",
            {"model_mode": model_mode},
        )],
    }


# ===========================================================================
# 四、条件路由函数
# ===========================================================================

@_normalize_state
def route_after_router(state: AgentState) -> str:
    """路由节点之后的条件分支。"""
    # 异常或要求结束 → end
    if state.get("error") or state.get("should_end"):
        return "end"

    intent = state.get("intent", INTENT_IRRELEVANT)
    if intent in (INTENT_SIMPLE_QA, INTENT_MATH_EXERCISE):
        return "qa"
    if intent in (INTENT_MATERIAL_RETRIEVE, INTENT_LITERATURE_ASSIST):
        return "retrieve"
    if intent == INTENT_COMPOSITE:
        return "composite_split"
    # irrelevant
    return "end"


@_normalize_state
def route_after_composite_split(state: AgentState) -> str:
    """复合任务拆分后，路由到第一个子任务。"""
    sub_tasks = state.get("sub_tasks", []) or []
    if not sub_tasks:
        return "end"

    first_task = sub_tasks[0]
    # 兼容 SubTask 实例与 dict（model_dump 后为 dict）
    task_type = first_task.task_type if hasattr(first_task, "task_type") \
        else first_task.get("task_type", "")
    if task_type == "qa":
        return "qa"
    if task_type in ("retrieve", "literature"):
        return "retrieve"
    return "end"


@_normalize_state
def route_after_task(state: AgentState) -> str:
    """任务推进节点之后，判断下一步走向。"""
    if state.get("error"):
        return "end"

    sub_tasks = state.get("sub_tasks", []) or []
    idx = state.get("current_task_index", 0)

    # 单任务场景：sub_tasks 为空 → reflection
    if not sub_tasks:
        return "reflection"

    # 复合场景：idx 已在 task_advance_node 中推进
    if idx >= len(sub_tasks):
        return "reflection"

    # 还有子任务
    next_task = sub_tasks[idx]
    # 兼容 SubTask 实例与 dict（model_dump 后为 dict）
    task_type = next_task.task_type if hasattr(next_task, "task_type") \
        else next_task.get("task_type", "")
    if task_type == "qa":
        return "qa"
    if task_type in ("retrieve", "literature"):
        return "retrieve"
    return "end"


# ===========================================================================
# 五、构建工作流图
# ===========================================================================

def build_workflow():
    """
    构建 LangGraph 工作流并编译。

    Returns:
        compiled_graph: 可直接调用 .invoke() / .stream() 的编译图。
    """
    builder = StateGraph(AgentState)

    # --- 注册节点 ---
    builder.add_node("router", router_node)
    builder.add_node("composite_split", composite_split_node)
    builder.add_node("qa", qa_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("task_advance", task_advance_node)
    builder.add_node("reflection", reflection_node)
    builder.add_node("end", end_node)

    # --- 入口 ---
    builder.add_edge(START, "router")

    # --- 路由节点条件分支 ---
    builder.add_conditional_edges(
        "router",
        route_after_router,
        {
            "qa": "qa",
            "retrieve": "retrieve",
            "composite_split": "composite_split",
            "end": "end",
        },
    )

    # --- 复合拆分后路由到首个子任务 ---
    builder.add_conditional_edges(
        "composite_split",
        route_after_composite_split,
        {
            "qa": "qa",
            "retrieve": "retrieve",
            "end": "end",
        },
    )

    # --- qa / retrieve 执行后统一进入任务推进节点 ---
    builder.add_edge("qa", "task_advance")
    builder.add_edge("retrieve", "task_advance")

    # --- 任务推进后条件分支：下一子任务 / 反思 / 结束 ---
    builder.add_conditional_edges(
        "task_advance",
        route_after_task,
        {
            "qa": "qa",
            "retrieve": "retrieve",
            "reflection": "reflection",
            "end": "end",
        },
    )

    # --- 反思 → 结束 ---
    builder.add_edge("reflection", "end")

    # --- 结束 → 终点 ---
    builder.add_edge("end", END)

    return builder.compile()


# ===========================================================================
# 六、便捷调用入口
# ===========================================================================

# 编译图单例（首次调用 build_workflow 时构建）
_compiled_graph = None


def get_graph():
    """获取编译后的工作流单例。"""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_workflow()
    return _compiled_graph


def run_query(user_query: str, history: Optional[list] = None) -> Dict[str, Any]:
    """
    端到端调用示例：直接调用 graph.invoke() 实现完整问答。

    Args:
        user_query: 用户问题
        history: 历史对话（可选）

    Returns:
        完整 state 字典，包含 final_answer / execution_log / retrieval_chunks 等

    用法：
        from graph.workflow import run_query
        result = run_query("求解方程 x^2 - 5x + 6 = 0")
        print(result["final_answer"])
    """
    graph = get_graph()
    model_mode = "cloud" if is_cloud_mode() else "local"
    initial_state = {
        "user_query": user_query,
        "history": history or [],
        "model_mode": model_mode,
    }
    logger.info(f"[run_query] 启动工作流 ({model_mode}模式): {user_query[:80]}")
    start = time.time()
    final_state = graph.invoke(initial_state)
    elapsed = time.time() - start
    logger.info(f"[run_query] 工作流完成 ({model_mode}模式)，耗时 {elapsed:.2f}s")
    return final_state


# ===========================================================================
# 七、自测入口
# ===========================================================================

def main() -> None:
    """workflow.py 自测：3 类典型输入端到端验证。"""
    print("=" * 70)
    print("  LangGraph 工作流自测")
    print("=" * 70)

    test_cases = [
        ("求解方程 x^2 - 5x + 6 = 0", "math_exercise"),
        ("什么是牛顿第二定律？", "simple_qa"),
        ("今天天气怎么样？", "irrelevant"),
    ]

    for user_input, expected_intent in test_cases:
        print(f"\n{'=' * 70}")
        print(f"[输入] {user_input}")
        print(f"[期望] intent={expected_intent}")
        print(f"{'=' * 70}")

        result = run_query(user_input)
        print(f"\n[路由意图] {result.get('intent')}")
        print(f"[流转日志] {len(result.get('execution_log', []))} 条:")
        for log in result.get("execution_log", []):
            print(f"  - {log['node']:>15} | {log['status']:>5} | {log['message']}")

        print(f"\n[召回片段] {len(result.get('retrieval_chunks', []))} 条")
        print(f"\n[最终答案]\n{result.get('final_answer', '')[:500]}")


if __name__ == "__main__":
    main()
