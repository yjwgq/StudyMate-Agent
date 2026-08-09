import sys
import os
import subprocess
import platform
import json
from typing import List, Tuple

REQUIRED_PYTHON_VERSION = (3, 12)
REQUIRED_DEPENDENCIES = [
    ("chromadb", "0.5.15"),
    ("fastapi", "0.140.0"),
    ("langchain", "1.3.14"),
    ("langchain-ollama", "1.1.0"),
    ("langgraph", "1.2.9"),
    ("loguru", "0.7.3"),
    ("ollama", "0.6.2"),
    ("openai", "1.60.0"),
    ("pydantic", "2.13.4"),
    ("python-dotenv", "1.2.2"),
    ("restrictedpython", "8.4"),
    ("rich", "15.0.0"),
    ("streamlit", "1.40.0"),
    ("sympy", "1.14.0"),
    ("uvicorn", "0.51.0"),
]
REQUIRED_MODELS = ["qwen3:4b", "qwen3-embedding:4b"]
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
USE_CLOUD_MODEL = os.getenv("USE_CLOUD_MODEL", "false").lower() == "true"


def check_python_version() -> Tuple[bool, str]:
    version = sys.version_info
    required_str = f"{REQUIRED_PYTHON_VERSION[0]}.{REQUIRED_PYTHON_VERSION[1]}"
    current_str = f"{version.major}.{version.minor}.{version.micro}"
    
    if version >= REQUIRED_PYTHON_VERSION:
        return True, f"✓ Python版本: {current_str} (要求: >= {required_str})"
    else:
        return False, f"✗ Python版本: {current_str} (要求: >= {required_str})"


def check_uv_installed() -> Tuple[bool, str]:
    try:
        result = subprocess.run(["uv", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            version = result.stdout.strip()
            return True, f"✓ uv已安装: {version}"
        else:
            return False, "✗ uv未安装"
    except FileNotFoundError:
        return False, "✗ uv未找到，请检查PATH环境变量"


def check_ollama_connection() -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            ["curl", "-s", f"{OLLAMA_HOST}/api/tags"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return True, f"✓ Ollama服务连通: {OLLAMA_HOST}"
        else:
            return False, f"✗ Ollama服务未连通: {OLLAMA_HOST}"
    except subprocess.TimeoutExpired:
        return False, f"✗ Ollama连接超时: {OLLAMA_HOST}"
    except FileNotFoundError:
        return False, "✗ curl未安装，无法检查Ollama连通性"


def check_ollama_models() -> List[Tuple[bool, str]]:
    results = []
    try:
        result = subprocess.run(
            ["curl", "-s", f"{OLLAMA_HOST}/api/tags"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            import json
            try:
                data = json.loads(result.stdout)
                models = [m["name"] for m in data.get("models", [])]
                
                for model in REQUIRED_MODELS:
                    if model in models:
                        results.append((True, f"✓ 模型存在: {model}"))
                    else:
                        results.append((False, f"✗ 模型缺失: {model} (执行: ollama pull {model})"))
            except json.JSONDecodeError:
                results.append((False, "✗ 无法解析Ollama响应"))
        else:
            for model in REQUIRED_MODELS:
                results.append((False, f"✗ 无法检查模型: {model} (Ollama未连通)"))
    except Exception as e:
        for model in REQUIRED_MODELS:
            results.append((False, f"✗ 检查模型失败: {model} ({str(e)})"))
    
    return results


def check_dependencies() -> List[Tuple[bool, str]]:
    results = []
    
    for package, version in REQUIRED_DEPENDENCIES:
        try:
            import importlib.metadata
            try:
                installed_version = importlib.metadata.version(package)
                if installed_version == version:
                    results.append((True, f"✓ {package}: {installed_version}"))
                else:
                    results.append((False, f"✗ {package}: {installed_version} (要求: {version})"))
            except importlib.metadata.PackageNotFoundError:
                results.append((False, f"✗ {package}: 未安装"))
        except ImportError:
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "show", package],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    for line in result.stdout.split("\n"):
                        if line.startswith("Version:"):
                            installed_version = line.split(":")[1].strip()
                            if installed_version == version:
                                results.append((True, f"✓ {package}: {installed_version}"))
                            else:
                                results.append((False, f"✗ {package}: {installed_version} (要求: {version})"))
                            break
                    else:
                        results.append((False, f"✗ {package}: 版本未知"))
                else:
                    results.append((False, f"✗ {package}: 未安装"))
            except Exception as e:
                results.append((False, f"✗ {package}: 检查失败 ({str(e)})"))
    
    return results


def check_env_file() -> Tuple[bool, str]:
    env_path = ".env"
    if os.path.exists(env_path):
        return True, "✓ .env配置文件存在"
    else:
        return False, "✗ .env配置文件缺失"


def check_chroma_directory() -> Tuple[bool, str]:
    chroma_path = os.getenv("CHROMA_PERSIST_DIRECTORY", "./chroma_db/")
    abs_path = os.path.abspath(chroma_path)
    
    if os.path.exists(chroma_path):
        if os.path.isdir(chroma_path):
            return True, f"✓ Chroma持久化目录: {abs_path}"
        else:
            return False, f"✗ Chroma路径不是目录: {abs_path}"
    else:
        try:
            os.makedirs(chroma_path)
            return True, f"✓ Chroma目录已创建: {abs_path}"
        except Exception as e:
            return False, f"✗ 无法创建Chroma目录: {abs_path} ({str(e)})"


def check_cloud_api_key() -> Tuple[bool, str]:
    api_key = os.getenv("CLOUD_API_KEY", "")
    if api_key and api_key != "your-cloud-api-key":
        return True, f"✓ 云端API密钥已配置"
    else:
        return False, f"✗ 云端API密钥未配置或使用默认值"


def check_cloud_connectivity() -> Tuple[bool, str]:
    base_url = os.getenv("CLOUD_LLM_BASE_URL", "")
    api_key = os.getenv("CLOUD_API_KEY", "")
    
    if not base_url or base_url == "https://api.example.com/v1":
        return False, f"✗ 云端API地址未配置"
    
    if not api_key or api_key == "your-cloud-api-key":
        return False, f"✗ 云端API密钥未配置，无法测试连通性"
    
    try:
        import urllib.request
        import urllib.error
        
        url = f"{base_url.rstrip('/')}/models"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True, f"✓ 云端API连通: {base_url}"
            else:
                return False, f"✗ 云端API响应异常: HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, f"✗ 云端API认证失败: 密钥无效"
        elif e.code == 403:
            return False, f"✗ 云端API权限不足: 额度耗尽或访问受限"
        else:
            return False, f"✗ 云端API请求失败: HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"✗ 云端API连接失败: {str(e)}"
    except Exception as e:
        return False, f"✗ 云端API测试异常: {str(e)}"


def check_port_available(port: int) -> Tuple[bool, str]:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("localhost", port))
            return True, f"✓ 端口{port}可用"
        except OSError:
            return False, f"✗ 端口{port}被占用"


def main():
    print("=" * 60)
    print("  StudyMate Agent - 环境自检")
    print("=" * 60)
    print()
    
    all_passed = True
    
    print("[基础环境]")
    print("-" * 30)
    
    passed, msg = check_python_version()
    print(msg)
    all_passed &= passed
    
    passed, msg = check_uv_installed()
    print(msg)
    all_passed &= passed
    
    passed, msg = check_env_file()
    print(msg)
    all_passed &= passed
    
    print()
    print(f"[模型模式] {'云端模式' if USE_CLOUD_MODEL else '本地模式'}")
    print("-" * 30)
    
    if USE_CLOUD_MODEL:
        print(f"  ✓ 模式开关: USE_CLOUD_MODEL={USE_CLOUD_MODEL}")
        
        passed, msg = check_cloud_api_key()
        print(msg)
        all_passed &= passed
        
        passed, msg = check_cloud_connectivity()
        print(msg)
        all_passed &= passed
    else:
        print(f"  ✓ 模式开关: USE_CLOUD_MODEL={USE_CLOUD_MODEL}")
        print("  跳过云端检测，启用本地Ollama模式")
        
        print()
        print("[Ollama服务]")
        print("-" * 30)
        
        passed, msg = check_ollama_connection()
        print(msg)
        all_passed &= passed
        
        for passed, msg in check_ollama_models():
            print(msg)
            all_passed &= passed
    
    print()
    print("[核心依赖]")
    print("-" * 30)
    
    for passed, msg in check_dependencies():
        print(msg)
        all_passed &= passed
    
    print()
    print("[端口检查]")
    print("-" * 30)
    
    passed, msg = check_port_available(8000)
    print(msg)
    all_passed &= passed
    
    passed, msg = check_port_available(8501)
    print(msg)
    all_passed &= passed
    
    if not USE_CLOUD_MODEL:
        passed, msg = check_port_available(11434)
        print(msg)
        all_passed &= passed
    
    print()
    print("[Chroma向量库]")
    print("-" * 30)
    
    passed, msg = check_chroma_directory()
    print(msg)
    all_passed &= passed
    
    print()
    print("=" * 60)
    if all_passed:
        print("  ✅ 所有检查通过！")
        print("=" * 60)
        print()
        if USE_CLOUD_MODEL:
            print("当前模式: 云端API模型")
            print("启动服务:")
            print("  - 后端: start_server.bat")
            print("  - 前端: start_front.bat")
        else:
            print("当前模式: 本地Ollama模型")
            print("启动服务:")
            print("  - 后端: start_server.bat")
            print("  - 前端: start_front.bat")
        return 0
    else:
        print("  ❌ 部分检查未通过，请修复后重试")
        print("=" * 60)
        print()
        print("修复建议:")
        if USE_CLOUD_MODEL:
            print("  1. 配置云端API密钥: 修改 .env 中的 CLOUD_API_KEY")
            print("  2. 配置云端API地址: 修改 .env 中的 CLOUD_LLM_BASE_URL")
            print("  3. 安装缺失依赖: uv sync")
        else:
            print("  1. 安装缺失依赖: uv sync")
            print("  2. 拉取Ollama模型: ollama pull qwen3:4b && ollama pull qwen3-embedding:4b")
            print("  3. 释放占用端口: 关闭占用8000/8501/11434的程序")
        return 1


if __name__ == "__main__":
    sys.exit(main())