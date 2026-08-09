# -*- coding: utf-8 -*-
import os
import time
import json
import traceback
from typing import List, Dict, Optional, Tuple
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document

try:
    from agents.base_agent import get_embedding_model, is_cloud_mode
except ImportError:
    import sys
    sys.path.append(str(Path(__file__).parent.parent))
    from agents.base_agent import get_embedding_model, is_cloud_mode


class ChromaVectorSearch:
    """Chroma向量库封装类，包含完整CRUD和Windows文件锁重试机制"""
    
    def __init__(self, persist_directory: str = "./chroma_db", 
                 collection_name: str = "studymate_kb",
                 embedding_model: Optional[str] = None,
                 ollama_host: Optional[str] = None):
        """
        初始化Chroma向量库
        
        Args:
            persist_directory: 向量库持久化目录
            collection_name: 集合名称
            embedding_model: 嵌入模型名称（本地模式），None表示使用环境变量默认值
            ollama_host: Ollama服务地址（本地模式），None表示使用环境变量默认值
        """
        self.persist_directory = str(Path(persist_directory).resolve())
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self.ollama_host = ollama_host
        
        self._create_persist_dir()
        self._init_embeddings()
        self._init_client()
    
    def _create_persist_dir(self):
        """创建持久化目录"""
        os.makedirs(self.persist_directory, exist_ok=True)
    
    def _init_embeddings(self):
        """初始化嵌入模型，自动适配本地/Ollama或云端/OpenAI兼容模式"""
        try:
            self.embeddings = get_embedding_model(model=self.embedding_model)
            if is_cloud_mode():
                print(f"✓ 使用云端嵌入模型")
            else:
                print(f"✓ 使用本地嵌入模型: {self.embedding_model or os.getenv('OLLAMA_EMBED_MODEL', 'qwen3-embedding:4b')}")
        except Exception as e:
            print(f"✗ 初始化嵌入模型失败: {type(e).__name__}: {str(e)}")
            if is_cloud_mode():
                print("  建议：检查CLOUD_API_KEY和CLOUD_EMBED_BASE_URL配置，或切换到本地模式")
            raise
    
    def _init_client(self, max_retries: int = 3, retry_delay: int = 5):
        """初始化Chroma客户端，带文件锁重试"""
        for attempt in range(max_retries):
            try:
                self.db = Chroma(
                    collection_name=self.collection_name,
                    embedding_function=self.embeddings,
                    persist_directory=self.persist_directory
                )
                print(f"✓ 成功连接Chroma向量库: {self.persist_directory}")
                return
            except Exception as e:
                if "lock" in str(e).lower() or "access denied" in str(e).lower():
                    print(f"⚠️ Chroma数据库锁定，第{attempt+1}/{max_retries}次重试...")
                    time.sleep(retry_delay)
                else:
                    raise
    
        raise RuntimeError(f"无法连接Chroma向量库，已重试{max_retries}次")
    
    def add_documents(self, documents: List[Dict], max_retries: int = 3) -> List[str]:
        """
        批量添加文档到向量库（单批次）
        
        Args:
            documents: 文档列表，每个文档包含 content 和 metadata
            max_retries: 重试次数
        
        Returns:
            添加的文档ID列表
        """
        if not documents:
            return []
        
        for attempt in range(max_retries):
            try:
                contents = [doc.get("content", "") for doc in documents]
                metadatas = [doc.get("metadata", {}) for doc in documents]
                ids = [doc.get("id", f"doc_{i}") for i, doc in enumerate(documents)]
                
                langchain_docs = [
                    Document(page_content=c, metadata=m) 
                    for c, m in zip(contents, metadatas)
                ]
                
                added_ids = self.db.add_documents(
                    documents=langchain_docs,
                    ids=ids
                )
                
                return added_ids
            except Exception as e:
                if "lock" in str(e).lower() or "access denied" in str(e).lower():
                    print(f"⚠️ 数据库锁定，重试 {attempt+1}/{max_retries}")
                    time.sleep(5)
                    self._init_client()
                elif is_cloud_mode() and ("timeout" in str(e).lower() or "network" in str(e).lower()):
                    print(f"⚠️ 云端API超时，重试 {attempt+1}/{max_retries}")
                    time.sleep(3)
                else:
                    print(f"✗ 添加文档失败: {type(e).__name__}: {str(e)}")
                    traceback.print_exc()
                    if is_cloud_mode():
                        print("  建议：检查网络连接或切换到本地模式")
                    raise
    
    def add_texts(self, texts: List[str], metadatas: Optional[List[Dict]] = None, 
                  ids: Optional[List[str]] = None) -> List[str]:
        """
        简化的文本添加接口
        
        Args:
            texts: 文本内容列表
            metadatas: 元数据列表（可选）
            ids: 文档ID列表（可选）
        
        Returns:
            添加的文档ID列表
        """
        documents = []
        for i, text in enumerate(texts):
            doc = {
                "content": text,
                "metadata": metadatas[i] if metadatas else {},
                "id": ids[i] if ids else f"doc_{i}"
            }
            documents.append(doc)
        return self.add_documents(documents)
    
    def search(self, query: str, k: int = 5, 
               filter: Optional[Dict] = None,
               score_threshold: Optional[float] = None) -> List[Dict]:
        """
        向量相似度检索
        
        Args:
            query: 查询文本
            k: 返回数量
            filter: 元数据过滤条件
            score_threshold: 相似度分数阈值
        
        Returns:
            检索结果列表，包含 content、score、metadata
        """
        max_retries = 3 if is_cloud_mode() else 1
        
        for attempt in range(max_retries):
            try:
                results = self.db.similarity_search_with_score(
                    query=query,
                    k=k,
                    filter=filter
                )
                
                formatted_results = []
                for doc, score in results:
                    if score_threshold is not None and score > score_threshold:
                        continue
                    
                    result = {
                        "content": doc.page_content,
                        "score": float(score),
                        "metadata": doc.metadata
                    }
                    formatted_results.append(result)
                
                return formatted_results
            except Exception as e:
                if is_cloud_mode() and attempt < max_retries - 1 and ("timeout" in str(e).lower() or "network" in str(e).lower()):
                    print(f"⚠️ 云端检索超时，重试 {attempt+1}/{max_retries}")
                    time.sleep(3)
                else:
                    print(f"✗ 检索失败: {str(e)}")
                    if is_cloud_mode():
                        print("  建议：检查网络连接或切换到本地模式")
                    return []
    
    def get_document_by_id(self, doc_id: str) -> Optional[Dict]:
        """根据ID获取文档"""
        try:
            docs = self.db.get(ids=[doc_id])
            if docs and docs["documents"]:
                return {
                    "content": docs["documents"][0],
                    "metadata": docs["metadatas"][0] if docs["metadatas"] else {},
                    "id": docs["ids"][0]
                }
            return None
        except Exception as e:
            print(f"✗ 获取文档失败: {str(e)}")
            return None
    
    def delete_documents(self, doc_ids: List[str], max_retries: int = 3) -> bool:
        """
        删除指定文档
        
        Args:
            doc_ids: 文档ID列表
            max_retries: 重试次数
        
        Returns:
            是否删除成功
        """
        for attempt in range(max_retries):
            try:
                self.db.delete(ids=doc_ids)
                print(f"✓ 删除 {len(doc_ids)} 条文档")
                return True
            except Exception as e:
                if "lock" in str(e).lower() or "access denied" in str(e).lower():
                    print(f"⚠️ 删除失败，数据库锁定，重试 {attempt+1}/{max_retries}")
                    time.sleep(5)
                else:
                    print(f"✗ 删除失败: {str(e)}")
                    return False
        
        return False
    
    def clear_collection(self, max_retries: int = 3) -> bool:
        """
        清空整个集合
        
        Args:
            max_retries: 重试次数
        
        Returns:
            是否清空成功
        """
        for attempt in range(max_retries):
            try:
                doc_ids = self.db.get()["ids"]
                if doc_ids:
                    self.db.delete(ids=doc_ids)
                    print(f"✓ 清空集合，共删除 {len(doc_ids)} 条文档")
                else:
                    print("✓ 集合已为空")
                return True
            except Exception as e:
                if "lock" in str(e).lower() or "access denied" in str(e).lower():
                    print(f"⚠️ 清空失败，数据库锁定，重试 {attempt+1}/{max_retries}")
                    time.sleep(5)
                else:
                    print(f"✗ 清空失败: {str(e)}")
                    return False
        
        return False
    
    def get_collection_stats(self) -> Dict:
        """获取集合统计信息"""
        try:
            collection = self.db._collection
            embed_model_info = "云端嵌入模型" if is_cloud_mode() else (self.embedding_model or os.getenv('OLLAMA_EMBED_MODEL', 'qwen3-embedding:4b'))
            return {
                "collection_name": self.collection_name,
                "document_count": collection.count(),
                "persist_directory": self.persist_directory,
                "embedding_model": embed_model_info,
                "model_mode": "cloud" if is_cloud_mode() else "local"
            }
        except Exception as e:
            print(f"✗ 获取统计信息失败: {str(e)}")
            return {}
    
    def has_collection(self) -> bool:
        """检查集合是否存在"""
        try:
            from chromadb.config import Settings
            import chromadb
            
            client = chromadb.PersistentClient(path=self.persist_directory)
            collections = client.list_collections()
            return any(col.name == self.collection_name for col in collections)
        except Exception as e:
            return False
    
    def create_collection(self, overwrite: bool = False) -> bool:
        """
        创建新集合
        
        Args:
            overwrite: 是否覆盖已存在的集合
        
        Returns:
            是否创建成功
        """
        if self.has_collection():
            if overwrite:
                self.clear_collection()
                print("✓ 已覆盖现有集合")
            else:
                print("✓ 集合已存在")
                return True
        
        try:
            self._init_client()
            print(f"✓ 创建集合: {self.collection_name}")
            return True
        except Exception as e:
            print(f"✗ 创建集合失败: {str(e)}")
            return False


def main():
    """测试向量库基本功能"""
    vector_search = ChromaVectorSearch()
    
    print("===== Chroma向量库测试 =====")
    
    stats = vector_search.get_collection_stats()
    print(f"集合统计: {json.dumps(stats, ensure_ascii=False, indent=2)}")
    
    test_docs = [
        {
            "content": "勾股定理是直角三角形两直角边的平方和等于斜边的平方，即a² + b² = c²",
            "metadata": {"source": "math", "type": "knowledge", "chapter": "几何"},
            "id": "test_doc_1"
        },
        {
            "content": "牛顿第二定律描述了力、质量和加速度之间的关系，F = ma",
            "metadata": {"source": "physics", "type": "knowledge", "chapter": "力学"},
            "id": "test_doc_2"
        },
        {
            "content": "光合作用是植物将光能转化为化学能的过程，6CO₂ + 6H₂O → C₆H₁₂O₆ + 6O₂",
            "metadata": {"source": "biology", "type": "knowledge", "chapter": "植物学"},
            "id": "test_doc_3"
        }
    ]
    
    print("\n1. 添加测试文档:")
    vector_search.add_documents(test_docs)
    
    print("\n2. 测试检索 '勾股定理':")
    results = vector_search.search("勾股定理", k=2)
    for i, result in enumerate(results):
        print(f"  结果 {i+1}: 分数={result['score']:.4f}, 来源={result['metadata'].get('source')}")
    
    print("\n3. 测试元数据过滤 (source=physics):")
    results = vector_search.search("力", k=2, filter={"source": "physics"})
    for i, result in enumerate(results):
        print(f"  结果 {i+1}: 分数={result['score']:.4f}, 内容={result['content'][:50]}...")
    
    print("\n4. 测试删除文档:")
    vector_search.delete_documents(["test_doc_1"])
    
    print("\n5. 测试清空集合:")
    vector_search.clear_collection()
    
    print("\n✅ 向量库测试完成！")


if __name__ == "__main__":
    main()