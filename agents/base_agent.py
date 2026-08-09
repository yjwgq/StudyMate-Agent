# -*- coding: utf-8 -*-
"""
BaseAgent 基础父类
==================
统一封装：
1. Ollama qwen3:4b LLM 初始化（langchain-ollama 标准写法）
2. 全局系统提示词模板
3. 日志工具（loguru，Windows 路径兼容）
4. 异常捕获装饰器

依据：项目硬性规则第3、7条——LLM 通过 Ollama 调用，qwen3:4b，所有工具调用遵循 langchain-ollama 标准写法。
"""
from __future__ import annotations

import functools
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from dotenv import load_dotenv
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from loguru import logger

# 加载 .env 配置文件
load_dotenv()

try:
    from langchain_ollama import ChatOllama, OllamaEmbeddings
except ImportError:
    ChatOllama = None
    OllamaEmbeddings = None

try:
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from langchain_openai.embeddings import OpenAIEmbeddings as _OpenAIEmbeddingsBase
except ImportError:
    ChatOpenAI = None
    OpenAIEmbeddings = None
    _OpenAIEmbeddingsBase = None


# ---------------------------------------------------------------------------
# DashScope 专用嵌入类（适配阿里云 API 参数格式）
# ---------------------------------------------------------------------------

class DashScopeEmbeddings:
    """
    阿里云 DashScope 专用嵌入模型类。
    
    与标准 OpenAI 的区别：
    - DashScope 使用 `input` 参数，而不是 `input`
    - 需要特殊处理参数格式转换
    """
    
    def __init__(self, model: str, api_key: str, base_url: str, dimensions: int = 1024):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.dimensions = dimensions
        
        # 延迟导入，避免导入错误
        from openai import OpenAI
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
    
    def embed_query(self, text: str) -> list[float]:
        """嵌入单个查询文本。"""
        try:
            response = self.client.embeddings.create(
                model=self.model,
                input=text,  # DashScope 使用 input
                dimensions=self.dimensions,
            )
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"DashScope 嵌入查询失败: {e}")
            raise
    
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """嵌入多个文档文本，自动分批处理（DashScope 限制每批 10 条）。"""
        MAX_BATCH_SIZE = 10  # DashScope text-embedding-v4 批量上限
        
        all_embeddings = []
        
        # 分批处理
        for i in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[i:i + MAX_BATCH_SIZE]
            try:
                response = self.client.embeddings.create(
                    model=self.model,
                    input=batch,  # DashScope 使用 input
                    dimensions=self.dimensions,
                )
                batch_embeddings = [item.embedding for item in response.data]
                all_embeddings.extend(batch_embeddings)
                
                if i + MAX_BATCH_SIZE < len(texts):
                    logger.info(f"批量嵌入进度: {i + MAX_BATCH_SIZE}/{len(texts)}")
            except Exception as e:
                logger.error(f"DashScope 批量嵌入失败 (batch {i//MAX_BATCH_SIZE + 1}): {e}")
                # 回退到单条嵌入
                logger.info("回退到单条嵌入模式")
                for text in batch:
                    try:
                        emb = self.embed_query(text)
                        all_embeddings.append(emb)
                    except Exception as e2:
                        logger.error(f"单条嵌入失败: {e2}")
                        raise
        
        return all_embeddings


# ---------------------------------------------------------------------------
# 日志配置（loguru 单例，Windows 兼容）
# ---------------------------------------------------------------------------

_LOG_DIR = Path("./logs")
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / "agents.log"

# 移除默认 handler，避免重复输出
try:
    logger.remove()
except ValueError:
    pass

# 控制台彩色输出 + 文件持久化（按天滚动，保留 7 天）
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    backtrace=False,
    diagnose=False,
)
logger.add(
    str(_LOG_FILE),
    level="DEBUG",
    rotation="00:00",
    retention="7 days",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    backtrace=True,
    diagnose=False,
)


# ---------------------------------------------------------------------------
# 模型工厂函数（自动适配本地/云端模式）
# ---------------------------------------------------------------------------

def is_cloud_mode() -> bool:
    """判断当前是否为云端模式。"""
    return os.getenv("USE_CLOUD_MODEL", "false").lower() == "true"


def get_llm(
    model: Optional[str] = None,
    temperature: float = 0.3,
    timeout: int = 60,
) -> Union[ChatOllama, ChatOpenAI]:
    """
    统一模型工厂：根据配置自动返回本地/Ollama或云端/OpenAI兼容模型。
    
    Args:
        model: 模型名称，若为None则从环境变量读取默认值
        temperature: 温度参数
        timeout: 超时时间（秒）
    
    Returns:
        ChatOllama 或 ChatOpenAI 实例
    """
    if is_cloud_mode():
        api_key = os.getenv("CLOUD_API_KEY", "")
        base_url = os.getenv("CLOUD_LLM_BASE_URL", "")
        # 云端模式：优先使用 CLOUD_LLM_MODEL_NAME，忽略本地模型名默认值
        cloud_default = os.getenv("CLOUD_LLM_MODEL_NAME", "qwen-plus")
        # 如果传入的 model 是本地模型名（qwen3:4b 等），则使用云端默认值
        local_models = {"qwen3:4b", "qwen3-embedding:4b"}
        if model and model not in local_models:
            model_name = model
        else:
            model_name = cloud_default
        
        if not api_key or api_key == "your-cloud-api-key":
            raise ValueError("云端模式已启用，但 CLOUD_API_KEY 未配置或使用默认值")
        if not base_url:
            raise ValueError("云端模式已启用，但 CLOUD_LLM_BASE_URL 未配置")
        
        if ChatOpenAI is None:
            raise ImportError("云端模式需要 langchain-openai 依赖，请安装: uv add langchain-openai")
        
        logger.info(f"初始化云端 LLM: {model_name} @ {base_url}")
        return ChatOpenAI(
            model=model_name,
            temperature=temperature,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=3,
        )
    else:
        model_name = model or os.getenv("OLLAMA_LLM_MODEL", "qwen3:4b")
        base_url = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        
        if ChatOllama is None:
            raise ImportError("本地模式需要 langchain-ollama 依赖，请安装: uv add langchain-ollama")
        
        logger.info(f"初始化本地 Ollama LLM: {model_name} @ {base_url}")
        return ChatOllama(
            model=model_name,
            temperature=temperature,
            base_url=base_url,
            timeout=timeout,
        )


def get_embedding_model(
    model: Optional[str] = None,
) -> Union[OllamaEmbeddings, OpenAIEmbeddings, DashScopeEmbeddings]:
    """
    统一向量模型工厂：根据配置自动返回本地/Ollama或云端/OpenAI兼容嵌入模型。
    
    Args:
        model: 模型名称，若为None则从环境变量读取默认值
    
    Returns:
        OllamaEmbeddings / OpenAIEmbeddings / DashScopeEmbeddings 实例
    """
    if is_cloud_mode():
        api_key = os.getenv("CLOUD_API_KEY", "")
        base_url = os.getenv("CLOUD_EMBED_BASE_URL", "") or os.getenv("CLOUD_LLM_BASE_URL", "")
        # 云端模式：优先使用 CLOUD_EMBED_MODEL_NAME，忽略本地模型名默认值
        cloud_default = os.getenv("CLOUD_EMBED_MODEL_NAME", "text-embedding-3-small")
        # 如果传入的 model 是本地模型名（qwen3-embedding:4b 等），则使用云端默认值
        local_models = {"qwen3:4b", "qwen3-embedding:4b"}
        if model and model not in local_models:
            model_name = model
        else:
            model_name = cloud_default
        
        if not api_key or api_key == "your-cloud-api-key":
            raise ValueError("云端模式已启用，但 CLOUD_API_KEY 未配置或使用默认值")
        if not base_url:
            raise ValueError("云端模式已启用，但 CLOUD_EMBED_BASE_URL/CLOUD_LLM_BASE_URL 未配置")
        
        # 检测是否为阿里云 DashScope 服务
        is_dashscope = "dashscope.aliyuncs.com" in base_url.lower()
        
        if is_dashscope:
            # DashScope 专用：使用自定义嵌入类
            logger.info(f"初始化 DashScope Embedding: {model_name} @ {base_url}")
            dimensions = int(os.getenv("CLOUD_EMBED_DIMENSIONS", "1024"))
            return DashScopeEmbeddings(
                model=model_name,
                api_key=api_key,
                base_url=base_url,
                dimensions=dimensions,
            )
        else:
            # 标准 OpenAI 兼容
            if OpenAIEmbeddings is None:
                raise ImportError("云端模式需要 langchain-openai 依赖，请安装: uv add langchain-openai")
            
            logger.info(f"初始化云端 Embedding: {model_name} @ {base_url}")
            return OpenAIEmbeddings(
                model=model_name,
                api_key=api_key,
                base_url=base_url,
                max_retries=3,
            )
    else:
        model_name = model or os.getenv("OLLAMA_EMBED_MODEL", "qwen3-embedding:4b")
        base_url = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        
        if OllamaEmbeddings is None:
            raise ImportError("本地模式需要 langchain-ollama 依赖，请安装: uv add langchain-ollama")
        
        logger.info(f"初始化本地 Ollama Embedding: {model_name} @ {base_url}")
        return OllamaEmbeddings(
            model=model_name,
            base_url=base_url,
        )


# ---------------------------------------------------------------------------
# 全局系统提示词
# ---------------------------------------------------------------------------

GLOBAL_SYSTEM_PROMPT = """你是 StudyMate 智能学习助手。

【角色定位】
- 服务对象：中学生、大学生、自学者
- 核心能力：学科知识问答、习题讲解、资料检索、文献辅助、结果校验
- 性格特征：严谨、耐心、结构化表达，避免编造事实

【输出规范】
1. 所有回答使用标准中文（数学公式可保留 LaTeX 符号）；
2. 数值与公式结论必须有推导过程，禁止直接给答案；
3. 涉及外部知识必须基于检索到的资料，禁止编造引用；
4. 输出结构使用 Markdown 标题、列表、代码块组织，确保可读性；
5. 涉及代码执行时，必须通过安全沙箱（RestrictedPython + SymPy），禁止直接 eval。

【安全约束】
- 不回答与学习无关的请求（政治、暴力、隐私窃取等）；
- 不输出未经验证的医学/法律建议；
- 数理计算只允许调用 tools.code_sandbox 中的封装函数。
"""


# ---------------------------------------------------------------------------
# 异常捕获装饰器
# ---------------------------------------------------------------------------

def safe_call(default_return: Any = None, *, reraise: bool = False) -> Callable:
    """
    方法装饰器：捕获异常并记录日志。

    Args:
        default_return: 异常时返回的默认值。
        reraise: 是否重新抛出异常（用于不可恢复的场景）。
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            method_name = f"{func.__module__}.{func.__qualname__}"
            start = time.time()
            try:
                logger.debug(f"→ 调用 {method_name} args={args!r} kwargs={list(kwargs.keys())}")
                result = func(*args, **kwargs)
                elapsed = time.time() - start
                logger.debug(f"← {method_name} 完成 耗时={elapsed:.2f}s")
                return result
            except Exception as e:
                elapsed = time.time() - start
                logger.error(
                    f"✗ {method_name} 异常 ({elapsed:.2f}s): {type(e).__name__}: {e}\n"
                    f"{traceback.format_exc()}"
                )
                if reraise:
                    raise
                return default_return
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# BaseAgent 父类
# ---------------------------------------------------------------------------

class BaseAgent:
    """
    所有 Agent 的基础父类。

    统一提供：
    - self.llm              ChatOllama 实例（qwen3:4b）
    - self.system_prompt    全局系统提示词
    - self.logger           loguru 日志器
    - self.invoke_llm()     带异常捕获的 LLM 调用
    - self.chain()          构建 prompt | llm | parser 链
    """

    # 默认模型参数（子类可覆盖）
    DEFAULT_MODEL = "qwen3:4b"
    DEFAULT_TEMPERATURE = 0.3
    DEFAULT_BASE_URL = "http://localhost:11434"

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        base_url: Optional[str] = None,
        system_prompt: Optional[str] = None,
        extra_agent_prompt: str = "",
    ) -> None:
        """
        Args:
            model: Ollama 模型名，默认 qwen3:4b。
            temperature: 温度，默认 0.3（稳定输出）。
            base_url: Ollama 服务地址。
            system_prompt: 自定义系统提示词（None 表示使用全局）。
            extra_agent_prompt: 子 Agent 专属补充提示词，将拼接在全局提示词之后。
        """
        # 云端模式：默认使用云端模型名
        if is_cloud_mode():
            cloud_model = os.getenv("CLOUD_LLM_MODEL_NAME", "qwen-plus")
            self.model_name = model if model and model not in {"qwen3:4b", "qwen3-embedding:4b"} else cloud_model
            self.base_url = base_url or os.getenv("CLOUD_LLM_BASE_URL", "")
        else:
            self.model_name = model or self.DEFAULT_MODEL
            self.base_url = base_url or self.DEFAULT_BASE_URL
        self.temperature = temperature if temperature is not None else self.DEFAULT_TEMPERATURE

        # 组合系统提示词：全局 + 子 Agent 专属
        base_prompt = system_prompt if system_prompt is not None else GLOBAL_SYSTEM_PROMPT
        self.system_prompt = base_prompt
        if extra_agent_prompt:
            self.system_prompt = f"{base_prompt}\n\n{extra_agent_prompt}"

        self.logger = logger.bind(agent=self.__class__.__name__)

        # 初始化 LLM
        self._init_llm()

        self.logger.info(
            f"Agent 初始化完成: model={self.model_name}, temp={self.temperature}"
        )

    # ------------------------------------------------------------------
    # LLM 初始化（自动适配本地/云端模式）
    # ------------------------------------------------------------------
    def _init_llm(self) -> None:
        """初始化 LLM 实例，自动适配本地/Ollama 或云端/OpenAI 兼容模式。"""
        self.llm = get_llm(
            model=self.model_name,
            temperature=self.temperature,
            timeout=60,
        )

    # ------------------------------------------------------------------
    # 通用调用接口
    # ------------------------------------------------------------------
    @safe_call(default_return="")
    def invoke_llm(
        self,
        user_input: str,
        history: Optional[list] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """
        同步调用 LLM。

        Args:
            user_input: 用户输入文本。
            history: 历史对话列表，每项形如 {"role": "user"/"assistant", "content": "..."}.
            temperature: 单次调用覆盖温度。

        Returns:
            LLM 输出字符串。
        """
        messages = [("system", self.system_prompt)]

        if history:
            for turn in history:
                role = turn.get("role", "user")
                content = turn.get("content", "")
                if role == "user":
                    messages.append(("user", content))
                elif role == "assistant":
                    messages.append(("assistant", content))

        messages.append(("user", user_input))

        prompt = ChatPromptTemplate.from_messages(messages)

        # 单次覆盖温度
        llm = self.llm
        if temperature is not None and temperature != self.temperature:
            llm = get_llm(
                model=self.model_name,
                temperature=temperature,
                timeout=60,
            )

        chain = prompt | llm | StrOutputParser()
        result = chain.invoke({})
        self.logger.debug(f"LLM 输出长度: {len(result)} 字符")
        return result

    # ------------------------------------------------------------------
    # 构建 Chain（供子类扩展）
    # ------------------------------------------------------------------
    def build_chain(self, prompt_template: ChatPromptTemplate, output_parser=None):
        """构建 prompt | llm | parser 链。"""
        parser = output_parser or StrOutputParser()
        return prompt_template | self.llm | parser

    # ------------------------------------------------------------------
    # 子类必须实现的入口
    # ------------------------------------------------------------------
    def run(self, *args, **kwargs):
        """子类必须实现的核心执行入口。"""
        raise NotImplementedError(f"{self.__class__.__name__} 必须实现 run() 方法")


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------

def main() -> None:
    """BaseAgent 自测：验证 LLM 连接与日志输出。"""
    print("===== BaseAgent 自测 =====\n")
    agent = BaseAgent()

    print("[1] 模型信息:")
    print(f"  model = {agent.model_name}")
    print(f"  temperature = {agent.temperature}")
    print(f"  base_url = {agent.base_url}")

    print("\n[2] 简单对话测试（你好）：")
    response = agent.invoke_llm("你好，请用一句话介绍自己。")
    print(f"  LLM 回复: {response[:200]}{'...' if len(response) > 200 else ''}")

    print("\n[3] 异常捕获测试：")
    @safe_call(default_return="默认返回值")
    def _fail():
        raise RuntimeError("模拟异常")
    print(f"  返回: {_fail()}")

    print("\n✅ BaseAgent 自测完成！")


if __name__ == "__main__":
    main()
