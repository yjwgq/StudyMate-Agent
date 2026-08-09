# -*- coding: utf-8 -*-
import os
import sys
import json
import time
from pathlib import Path
from typing import List, Dict

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from tools.vector_search import ChromaVectorSearch
from data_process.doc_parser import DocumentParser
from data_process.text_splitter import AdaptiveTextSplitter


class BatchKBBuilder:
    """批量知识库构建器，分批向量化存入Chroma"""
    
    def __init__(self, persist_directory: str = "./chroma_db",
                 collection_name: str = "studymate_kb",
                 batch_size: int = 50,
                 embedding_model: str = None):
        """
        初始化批量构建器
        
        Args:
            persist_directory: 向量库持久化目录
            collection_name: 集合名称
            batch_size: 每批插入数量
            embedding_model: 嵌入模型名称，None则自动根据模式选择
        """
        self.persist_directory = persist_directory
        self.collection_name = collection_name
        self.batch_size = batch_size
        self.embedding_model = embedding_model
        
        self.vector_search = ChromaVectorSearch(
            persist_directory=persist_directory,
            collection_name=collection_name,
            embedding_model=embedding_model
        )
        self.doc_parser = DocumentParser()
        self.text_splitter = AdaptiveTextSplitter()
        
        self.DATA_ROOT = Path("./datasets")
        self.CLEAN_ROOT = self.DATA_ROOT / "cleaned"
    
    def _read_jsonl(self, file_path: str) -> List[Dict]:
        """读取jsonl文件"""
        items = []
        if not os.path.exists(file_path):
            print(f"警告：文件不存在 {file_path}")
            return items
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        items.append(json.loads(line))
            print(f"✓ 读取 {file_path}: {len(items)} 条")
        except Exception as e:
            print(f"✗ 读取文件失败 {file_path}: {str(e)}")
        
        return items
    
    def _prepare_dataset_docs(self, dataset_name: str, split: str) -> List[Dict]:
        """准备数据集文档"""
        docs = []
        file_path = self.CLEAN_ROOT / dataset_name / f"{split}.jsonl"
        items = self._read_jsonl(str(file_path))
        
        for idx, item in enumerate(items):
            content = ""
            metadata = {
                "source": item.get("source", dataset_name),
                "type": item.get("type", "question"),
                "split": item.get("split", split),
                "domain": item.get("domain", "")
            }
            
            if dataset_name == "gsm8k":
                question = item.get("question", "")
                reasoning = "\n".join(item.get("reasoning_steps", []))
                answer = str(item.get("final_answer", ""))
                content = f"问题：{question}\n\n推理过程：{reasoning}\n\n答案：{answer}"
                metadata["final_answer"] = answer
                
            elif dataset_name == "sciq":
                question = item.get("question", "")
                answer = item.get("correct_answer", "")
                support = item.get("support", "")
                distractors = ", ".join(item.get("distractors", []))
                content = f"问题：{question}\n\n选项：{distractors}\n\n正确答案：{answer}\n\n解析：{support}"
                metadata["correct_answer"] = answer
                
            elif dataset_name == "mmlu":
                question = item.get("question", "")
                choices = item.get("choices", [])
                answer = item.get("correct_answer", "")
                content = f"问题：{question}\n\n选项：{', '.join(choices)}\n\n正确答案：{answer}"
                metadata["correct_answer"] = answer
            
            if content.strip():
                docs.append({
                    "id": f"{dataset_name}_{split}_{idx:06d}",
                    "content": content.strip(),
                    "metadata": metadata
                })
        
        return docs
    
    def build_from_datasets(self, datasets: List[str] = None,
                            splits: List[str] = None) -> int:
        """
        从清洗后的数据集构建知识库
        
        Args:
            datasets: 数据集名称列表 ["gsm8k", "sciq", "mmlu"]
            splits: 数据分割列表 ["train", "test", "validation"]
        
        Returns:
            成功入库的文档数量
        """
        if datasets is None:
            datasets = ["gsm8k", "sciq", "mmlu"]
        
        if splits is None:
            splits = {"gsm8k": ["train", "test"],
                      "sciq": ["train", "validation", "test"],
                      "mmlu": ["dev", "test", "validation"]}
        
        total_added = 0
        
        for dataset in datasets:
            print(f"\n===== 处理数据集: {dataset} =====")
            dataset_splits = splits.get(dataset, ["train", "test"])
            
            for split in dataset_splits:
                docs = self._prepare_dataset_docs(dataset, split)
                if docs:
                    print(f"  准备 {split} 数据: {len(docs)} 条")
                    added = self._batch_add_docs(docs)
                    total_added += added
        
        return total_added
    
    def build_from_documents(self, directory: str,
                            strategy: str = "lecture") -> int:
        """
        从文档目录构建知识库
        
        Args:
            directory: 文档目录路径
            strategy: 分块策略 ["dataset", "lecture", "paper"]
        
        Returns:
            成功入库的文档数量
        """
        print(f"\n===== 从文档目录构建: {directory} =====")
        
        documents = self.doc_parser.batch_parse(directory)
        if not documents:
            print("  没有解析到文档")
            return 0
        
        chunks = self.text_splitter.split_documents(documents, strategy=strategy)
        print(f"  分块结果: {len(chunks)} 个chunk")
        
        docs = []
        for idx, chunk in enumerate(chunks):
            docs.append({
                "id": f"doc_{idx:06d}",
                "content": chunk.get("content", ""),
                "metadata": {
                    "source": chunk.get("metadata", {}).get("source", ""),
                    "type": "document",
                    "section_title": chunk.get("section_title", ""),
                    **chunk.get("metadata", {})
                }
            })
        
        return self._batch_add_docs(docs)
    
    def _batch_add_docs(self, docs: List[Dict]) -> int:
        """分批添加文档"""
        if not docs:
            return 0
        
        print(f"  开始分批入库，共 {len(docs)} 条，每批 {self.batch_size} 条")
        
        total_added = 0
        start_time = time.time()
        
        for i in range(0, len(docs), self.batch_size):
            batch = docs[i:i+self.batch_size]
            try:
                added_ids = self.vector_search.add_documents(batch)
                total_added += len(added_ids)
                
                progress = (i + len(batch)) / len(docs) * 100
                elapsed = time.time() - start_time
                print(f"    进度: {progress:.1f}% ({i + len(batch)}/{len(docs)}), 耗时: {elapsed:.1f}秒")
                
            except Exception as e:
                print(f"    ✗ 批次失败: {str(e)}")
                continue
        
        elapsed = time.time() - start_time
        print(f"  入库完成: {total_added} 条，耗时: {elapsed:.1f}秒")
        
        return total_added
    
    def build_full_kb(self) -> int:
        """构建完整知识库（数据集+文档）"""
        print("===== 开始构建完整知识库 =====")
        
        total_added = 0
        
        total_added += self.build_from_datasets()
        
        documents_dir = self.DATA_ROOT / "documents"
        if documents_dir.exists():
            total_added += self.build_from_documents(str(documents_dir))
        
        stats = self.vector_search.get_collection_stats()
        print(f"\n✅ 知识库构建完成！")
        print(f"   总入库文档数: {total_added}")
        print(f"   集合统计: {json.dumps(stats, ensure_ascii=False, indent=2)}")
        
        return total_added


def main():
    """批量构建知识库入口"""
    builder = BatchKBBuilder(batch_size=50)
    
    import argparse
    parser = argparse.ArgumentParser(description="批量构建知识库")
    parser.add_argument("--datasets", action="store_true", help="从数据集构建")
    parser.add_argument("--documents", type=str, default=None, help="从文档目录构建")
    parser.add_argument("--all", action="store_true", help="构建完整知识库")
    parser.add_argument("--batch-size", type=int, default=50, help="每批插入数量")
    
    args = parser.parse_args()
    
    if args.batch_size != 50:
        builder.batch_size = args.batch_size
    
    if args.all:
        builder.build_full_kb()
    elif args.datasets:
        builder.build_from_datasets()
    elif args.documents:
        builder.build_from_documents(args.documents)
    else:
        print("请指定构建方式：--datasets, --documents, 或 --all")


if __name__ == "__main__":
    main()