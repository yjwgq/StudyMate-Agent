# -*- coding: utf-8 -*-
import os
import json
from pathlib import Path

from tools.vector_search import ChromaVectorSearch


def init_chroma_collection(collection_name: str = "studymate_kb",
                           persist_directory: str = "./chroma_db",
                           overwrite: bool = False) -> bool:
    """
    初始化Chroma向量库集合
    
    Args:
        collection_name: 集合名称
        persist_directory: 持久化目录
        overwrite: 是否覆盖已存在的集合
    
    Returns:
        是否初始化成功
    """
    print("===== 初始化Chroma向量库 =====")
    
    persist_dir = Path(persist_directory).resolve()
    print(f"持久化目录: {persist_dir}")
    
    os.makedirs(persist_dir, exist_ok=True)
    
    try:
        vector_search = ChromaVectorSearch(
            persist_directory=str(persist_dir),
            collection_name=collection_name
        )
        
        if vector_search.create_collection(overwrite=overwrite):
            stats = vector_search.get_collection_stats()
            print(f"\n✅ 向量库初始化完成！")
            print(f"集合名称: {stats.get('collection_name', collection_name)}")
            print(f"文档数量: {stats.get('document_count', 0)}")
            print(f"持久化目录: {stats.get('persist_directory', persist_dir)}")
            print(f"嵌入模型: {stats.get('embedding_model', 'qwen3-embedding:4b')}")
            return True
        else:
            print("❌ 向量库初始化失败")
            return False
            
    except Exception as e:
        print(f"❌ 初始化失败: {str(e)}")
        return False


def check_ollama_connection():
    """检查Ollama连接"""
    print("\n===== 检查Ollama连接 =====")
    
    try:
        import requests
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        if response.status_code == 200:
            data = response.json()
            models = [m["name"] for m in data.get("models", [])]
            print(f"✓ Ollama服务连通")
            print(f"  已下载模型: {models}")
            
            required_models = ["qwen3:4b", "qwen3-embedding:4b"]
            missing = [m for m in required_models if m not in models]
            if missing:
                print(f"⚠️ 缺少模型: {missing}")
                print(f"   执行命令: ollama pull {' && ollama pull '.join(missing)}")
            else:
                print("✓ 所有必需模型已就绪")
            
            return True
        else:
            print(f"✗ Ollama服务响应异常: {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print("✗ Ollama服务未启动或无法连接")
        print("   请先启动Ollama: ollama serve")
        return False
    except Exception as e:
        print(f"✗ 检查Ollama失败: {str(e)}")
        return False


def main():
    """一键初始化入口"""
    import argparse
    parser = argparse.ArgumentParser(description="初始化Chroma向量库")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的集合")
    parser.add_argument("--check-only", action="store_true", help="仅检查环境")
    
    args = parser.parse_args()
    
    if args.check_only:
        check_ollama_connection()
        return
    
    check_ollama_connection()
    
    if init_chroma_collection(overwrite=args.overwrite):
        print("\n===== 初始化完成 =====")
        print("下一步操作:")
        print("  1. 运行清洗脚本: uv run python data_process/dataset_clean.py --clean")
        print("  2. 批量入库: uv run python data_process/batch_build_kb.py --all")
        print("  3. 测试检索: uv run python test_retrieve.py")
    else:
        print("\n❌ 初始化失败，请检查错误信息")


if __name__ == "__main__":
    main()