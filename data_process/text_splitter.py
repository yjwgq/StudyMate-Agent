# -*- coding: utf-8 -*-
import re
from typing import List, Dict, Tuple
from pathlib import Path


class AdaptiveTextSplitter:
    """自适应文本分块器，区分三类分片策略"""
    
    def __init__(self):
        self.strategies = {
            "dataset": {
                "chunk_size": 512,
                "chunk_overlap": 64,
                "separator": "\n\n",
                "title_pattern": None
            },
            "lecture": {
                "chunk_size": 1024,
                "chunk_overlap": 128,
                "separator": "\n\n",
                "title_pattern": re.compile(r'^第\s*\d+\s*[章节篇]\s*|^[一二三四五六七八九十]+[\s、.．]\s*')
            },
            "paper": {
                "chunk_size": 2048,
                "chunk_overlap": 256,
                "separator": "\n\n",
                "title_pattern": re.compile(r'^(摘要|abstract|引言|introduction|结论|conclusion|参考文献|references|附录|appendix)\s*', re.IGNORECASE)
            }
        }
    
    def count_tokens(self, text: str) -> int:
        """估算文本token数量（中文字符按1token计算）"""
        return len(text)
    
    def split_by_strategy(self, text: str, strategy: str = "dataset", 
                         chunk_size: int = None, chunk_overlap: int = None) -> List[Dict]:
        """根据策略拆分文本"""
        if strategy not in self.strategies:
            raise ValueError(f"不支持的策略: {strategy}")
        
        config = self.strategies[strategy].copy()
        if chunk_size is not None:
            config["chunk_size"] = chunk_size
        if chunk_overlap is not None:
            config["chunk_overlap"] = chunk_overlap
        
        chunks = []
        
        if config["title_pattern"]:
            chunks = self._split_by_title(text, config)
        else:
            chunks = self._split_by_size(text, config)
        
        return chunks
    
    def _split_by_size(self, text: str, config: Dict) -> List[Dict]:
        """按固定大小拆分"""
        chunk_size = config["chunk_size"]
        chunk_overlap = config["chunk_overlap"]
        separator = config["separator"]
        
        chunks = []
        sentences = text.split(separator)
        
        current_chunk = []
        current_length = 0
        
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            sentence_length = self.count_tokens(sentence)
            
            if current_length + sentence_length > chunk_size and current_chunk:
                chunk_text = separator.join(current_chunk)
                chunks.append({
                    "content": chunk_text,
                    "length": current_length,
                    "chunk_index": len(chunks)
                })
                
                overlap_text = separator.join(current_chunk[-2:]) if len(current_chunk) >= 2 else ""
                current_chunk = [overlap_text, sentence] if overlap_text else [sentence]
                current_length = self.count_tokens(separator.join(current_chunk))
            else:
                current_chunk.append(sentence)
                current_length += sentence_length + len(separator)
        
        if current_chunk:
            chunk_text = separator.join(current_chunk)
            chunks.append({
                "content": chunk_text,
                "length": current_length,
                "chunk_index": len(chunks)
            })
        
        return chunks
    
    def _split_by_title(self, text: str, config: Dict) -> List[Dict]:
        """按标题拆分（适用于课件、论文）"""
        chunk_size = config["chunk_size"]
        chunk_overlap = config["chunk_overlap"]
        separator = config["separator"]
        title_pattern = config["title_pattern"]
        
        lines = text.split('\n')
        sections = []
        current_section = {
            "title": "",
            "content": ""
        }
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            if title_pattern.match(line) and len(line) < 100:
                if current_section["content"].strip():
                    sections.append(current_section)
                current_section = {
                    "title": line,
                    "content": ""
                }
            else:
                current_section["content"] += line + '\n'
        
        if current_section["content"].strip():
            sections.append(current_section)
        
        chunks = []
        for section in sections:
            section_text = f"{section['title']}\n\n{section['content']}" if section['title'] else section['content']
            
            if self.count_tokens(section_text) <= chunk_size:
                chunks.append({
                    "content": section_text.strip(),
                    "length": self.count_tokens(section_text),
                    "chunk_index": len(chunks),
                    "section_title": section['title']
                })
            else:
                sub_chunks = self._split_by_size(section_text, config)
                for sub_chunk in sub_chunks:
                    sub_chunk["section_title"] = section['title']
                    sub_chunk["chunk_index"] = len(chunks)
                    chunks.append(sub_chunk)
        
        return chunks
    
    def split_dataset_items(self, items: List[Dict], content_field: str = "question") -> List[Dict]:
        """拆分数据集项，每条记录作为一个chunk"""
        chunks = []
        
        for idx, item in enumerate(items):
            content = item.get(content_field, "")
            if not content:
                continue
            
            chunk = {
                "content": content,
                "length": self.count_tokens(content),
                "chunk_index": idx,
                "metadata": {k: v for k, v in item.items() if k != content_field}
            }
            chunks.append(chunk)
        
        return chunks
    
    def split_documents(self, documents: List[Dict], strategy: str = "lecture") -> List[Dict]:
        """批量拆分文档"""
        all_chunks = []
        
        for doc in documents:
            full_text = doc.get("full_text", "")
            if not full_text:
                continue
            
            chunks = self.split_by_strategy(full_text, strategy)
            
            for chunk in chunks:
                chunk["metadata"] = {
                    "source": doc.get("file_name", ""),
                    "file_path": doc.get("file_path", ""),
                    **doc.get("metadata", {})
                }
                all_chunks.append(chunk)
        
        return all_chunks


def main():
    """测试文本分块器"""
    splitter = AdaptiveTextSplitter()
    
    test_text = """第一章 数学基础

1.1 代数运算
代数运算包括加法、减法、乘法和除法。这些基本运算构成了数学的基础。在日常生活中，我们经常使用这些运算来解决各种问题。

1.2 方程求解
方程是数学中的重要概念。通过解方程，我们可以找到未知数的值。一元一次方程的一般形式是ax + b = 0。

第二章 几何图形

2.1 三角形
三角形是由三条边组成的多边形。三角形的内角和等于180度。根据边长和角度，三角形可以分为不同类型。

2.2 圆形
圆形是平面上到定点距离等于定长的点的集合。圆的面积公式是S = πr²，周长公式是C = 2πr。"""
    
    print("===== 文本分块器测试 =====")
    
    print("\n1. 课件讲义分块 (lecture策略):")
    chunks = splitter.split_by_strategy(test_text, "lecture", chunk_size=200)
    for i, chunk in enumerate(chunks):
        print(f"  Chunk {i}: {chunk.get('section_title', '')} - {len(chunk['content'])}字符")
    
    print("\n2. 学术论文分块 (paper策略):")
    chunks = splitter.split_by_strategy(test_text, "paper", chunk_size=300)
    for i, chunk in enumerate(chunks):
        print(f"  Chunk {i}: {chunk.get('section_title', '')} - {len(chunk['content'])}字符")
    
    print("\n3. 习题数据集分块 (dataset策略):")
    test_items = [
        {"question": "小明有5个苹果，吃了2个，还剩几个？", "answer": 3, "source": "test"},
        {"question": "一个长方形长10cm，宽5cm，面积是多少？", "answer": 50, "source": "test"}
    ]
    chunks = splitter.split_dataset_items(test_items)
    for i, chunk in enumerate(chunks):
        print(f"  Chunk {i}: {chunk['content'][:30]}...")
    
    print("\n✅ 分块测试完成！")


if __name__ == "__main__":
    main()