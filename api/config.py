# -*- coding: utf-8 -*-
"""
api/config.py — 全局配置加载
============================
职责：
1. 读取项目根目录 .env 文件，统一管理 Ollama / Chroma / 服务端口 等参数；
2. 提供 Settings 单例（settings），供 main.py / schemas.py / 前端 api_client 复用；
3. 加载并暴露 Chroma 持久化路径，便于健康检测与文件锁冲突诊断。

依据：项目硬性规则——环境变量存储于 .env，Chroma 本地文件持久化目录
      默认 ./chroma_db/，Windows 路径使用 pathlib 兼容。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 1. 项目根路径定位（api/config.py → 项目根 = 上级目录）
# ---------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# 加载 .env（路径相对于项目根，避免工作目录差异导致读不到）
_ENV_PATH = PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    load_dotenv(dotenv_path=str(_ENV_PATH))


# ---------------------------------------------------------------------------
# 2. Settings 配置模型（pydantic BaseModel）
# ---------------------------------------------------------------------------
class Settings(BaseModel):
    """全局配置容器，所有字段均来自 .env，并提供安全默认值。"""

    # ---- Ollama 服务 ----
    ollama_host: str = Field(
        default="http://localhost:11434",
        description="Ollama 服务地址",
    )
    ollama_llm_model: str = Field(
        default="qwen3:4b",
        description="Ollama 主模型（LLM）",
    )
    ollama_embed_model: str = Field(
        default="qwen3-embedding:4b",
        description="Ollama 嵌入模型",
    )

    # ---- 云端模型配置 ----
    use_cloud_model: bool = Field(
        default=False,
        description="是否使用云端模型",
    )
    cloud_api_key: str = Field(
        default="",
        description="云端API密钥",
    )
    cloud_llm_base_url: str = Field(
        default="",
        description="云端大模型接口地址",
    )
    cloud_llm_model_name: str = Field(
        default="",
        description="云端对话模型名称",
    )
    cloud_embed_base_url: str = Field(
        default="",
        description="云端向量模型接口地址",
    )
    cloud_embed_model_name: str = Field(
        default="",
        description="云端嵌入模型名称",
    )
    cloud_embed_dimensions: int = Field(
        default=1024,
        description="云端嵌入向量维度",
    )

    # ---- Chroma 向量库 ----
    chroma_persist_directory: str = Field(
        default="./chroma_db/",
        description="Chroma 持久化目录（相对项目根）",
    )
    chroma_collection_name: str = Field(
        default="studymate_kb",
        description="Chroma 集合名称",
    )

    # ---- FastAPI 服务 ----
    fastapi_host: str = Field(
        default="0.0.0.0",
        description="FastAPI 监听地址",
    )
    fastapi_port: int = Field(
        default=8000,
        description="FastAPI 监听端口",
    )

    # ---- Streamlit 服务 ----
    streamlit_port: int = Field(
        default=8501,
        description="Streamlit 监听端口",
    )

    # ---- 接口超时（秒）----
    request_timeout: int = Field(
        default=300,
        description="单次请求最大处理秒数（多 Agent 流程较慢，给足时间）",
    )
    retrieve_timeout: int = Field(
        default=30,
        description="向量检索最大秒数",
    )
    calc_timeout: int = Field(
        default=20,
        description="沙箱计算最大秒数",
    )

    # ---- 单节点超时（秒）----
    router_timeout: int = Field(
        default=60,
        description="路由节点 LLM 调用最大秒数",
    )
    qa_timeout: int = Field(
        default=120,
        description="QA 节点（含检索+LLM）最大秒数",
    )
    retrieve_node_timeout: int = Field(
        default=90,
        description="检索节点（含检索+LLM）最大秒数",
    )
    reflection_timeout: int = Field(
        default=90,
        description="反思校验节点最大秒数",
    )

    # ---- 运行模式 ----
    app_mode: str = Field(
        default="separated",
        description="运行模式：separated=前后端分离 / integrated=一体化",
    )

    class Config:
        arbitrary_types_allowed = True


# ---------------------------------------------------------------------------
# 3. 单例实例化（从环境变量读取）
# ---------------------------------------------------------------------------
def _build_settings() -> Settings:
    """从环境变量构造 Settings 实例（带兜底默认值）。"""
    return Settings(
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        ollama_llm_model=os.getenv("OLLAMA_LLM_MODEL", "qwen3:4b"),
        ollama_embed_model=os.getenv("OLLAMA_EMBED_MODEL", "qwen3-embedding:4b"),
        use_cloud_model=os.getenv("USE_CLOUD_MODEL", "false").lower() == "true",
        cloud_api_key=os.getenv("CLOUD_API_KEY", ""),
        cloud_llm_base_url=os.getenv("CLOUD_LLM_BASE_URL", ""),
        cloud_llm_model_name=os.getenv("CLOUD_LLM_MODEL_NAME", ""),
        cloud_embed_base_url=os.getenv("CLOUD_EMBED_BASE_URL", ""),
        cloud_embed_model_name=os.getenv("CLOUD_EMBED_MODEL_NAME", ""),
        cloud_embed_dimensions=int(os.getenv("CLOUD_EMBED_DIMENSIONS", "1024")),
        chroma_persist_directory=os.getenv(
            "CHROMA_PERSIST_DIRECTORY", "./chroma_db/"
        ),
        chroma_collection_name=os.getenv("CHROMA_COLLECTION_NAME", "studymate_kb"),
        fastapi_host=os.getenv("FASTAPI_HOST", "0.0.0.0"),
        fastapi_port=int(os.getenv("FASTAPI_PORT", "8000")),
        streamlit_port=int(os.getenv("STREAMLIT_PORT", "8501")),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "300")),
        retrieve_timeout=int(os.getenv("RETRIEVE_TIMEOUT", "30")),
        calc_timeout=int(os.getenv("CALC_TIMEOUT", "20")),
        router_timeout=int(os.getenv("ROUTER_TIMEOUT", "60")),
        qa_timeout=int(os.getenv("QA_TIMEOUT", "120")),
        retrieve_node_timeout=int(os.getenv("RETRIEVE_NODE_TIMEOUT", "90")),
        reflection_timeout=int(os.getenv("REFLECTION_TIMEOUT", "90")),
        app_mode=os.getenv("APP_MODE", "separated"),
    )


settings: Settings = _build_settings()


# ---------------------------------------------------------------------------
# 4. Chroma 持久化路径工具（统一绝对路径，便于健康检测/锁冲突诊断）
# ---------------------------------------------------------------------------
def get_chroma_abs_path() -> Path:
    """
    获取 Chroma 持久化目录的绝对路径。

    - 若配置为相对路径，则相对于 PROJECT_ROOT 解析；
    - 若目录不存在，不主动创建（由调用方决定是否创建，避免误生成）。
    """
    chroma_path = Path(settings.chroma_persist_directory)
    if not chroma_path.is_absolute():
        chroma_path = PROJECT_ROOT / chroma_path
    return chroma_path.resolve()


def get_chroma_lock_files() -> list:
    """
    扫描 Chroma 目录下的 .lock 文件（用于诊断 Windows 文件锁冲突）。

    Returns:
        .lock 文件绝对路径列表（可能为空）
    """
    chroma_dir = get_chroma_abs_path()
    if not chroma_dir.exists():
        return []
    return [str(p) for p in chroma_dir.rglob("*.lock")]


def is_ollama_reachable(timeout: int = 3) -> bool:
    """
    快速探测 Ollama 服务是否在线（GET /api/tags）。

    使用标准库 urllib，避免引入 requests 依赖。
    自动处理 ollama_host 缺少 http:// 前缀的情况。
    """
    import json
    import urllib.error
    import urllib.request

    host = settings.ollama_host.strip()
    # 确保 URL 有协议前缀
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"

    url = f"{host.rstrip('/')}/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                json.loads(resp.read().decode("utf-8"))
                return True
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return False


# ---------------------------------------------------------------------------
# 5. 自测入口
# ---------------------------------------------------------------------------
def main() -> None:
    """config.py 自测：打印当前配置与 Chroma 路径状态。"""
    print("=" * 60)
    print("  StudyMate Agent — 后端配置自检")
    print("=" * 60)

    print(f"\n[项目根] {PROJECT_ROOT}")
    print(f"[.env]   {_ENV_PATH} ({'存在' if _ENV_PATH.exists() else '缺失'})")

    print("\n[Ollama]")
    print(f"  host        = {settings.ollama_host}")
    print(f"  llm_model   = {settings.ollama_llm_model}")
    print(f"  embed_model = {settings.ollama_embed_model}")
    print(f"  可达性      = {'✓ 在线' if is_ollama_reachable() else '✗ 离线'}")

    print("\n[Chroma]")
    chroma_path = get_chroma_abs_path()
    print(f"  持久化目录  = {chroma_path}")
    print(f"  目录存在    = {'✓' if chroma_path.exists() else '✗'}")
    print(f"  集合名称    = {settings.chroma_collection_name}")
    lock_files = get_chroma_lock_files()
    print(f"  .lock 文件  = {len(lock_files)} 个")
    for f in lock_files[:3]:
        print(f"    - {f}")

    print("\n[服务端口]")
    print(f"  FastAPI   = {settings.fastapi_host}:{settings.fastapi_port}")
    print(f"  Streamlit = localhost:{settings.streamlit_port}")
    print(f"  运行模式  = {settings.app_mode}")

    print("\n✅ 配置加载完成！")


if __name__ == "__main__":
    main()
