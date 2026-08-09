# -*- coding: utf-8 -*-
import os
import re
from pathlib import Path
from typing import List, Dict

try:
    from PyPDF2 import PdfReader
    HAS_PYPDF2 = True
except ImportError:
    HAS_PYPDF2 = False

try:
    from docx import Document
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False


class DocumentParser:
    """文档解析工具，支持PDF、docx批量读取，提取纯文本"""
    
    def __init__(self):
        self.header_patterns = [
            re.compile(r'^第\s*\d+\s*[章节篇]\s*', re.IGNORECASE),
            re.compile(r'^[一二三四五六七八九十]+[\s、.．]\s*', re.IGNORECASE),
            re.compile(r'^\d+[\s、.．]\s*', re.IGNORECASE),
            re.compile(r'^[\d]+\.[\d]+[\s、.．]\s*', re.IGNORECASE),
        ]
        
        self.footer_patterns = [
            re.compile(r'^\s*-\s*\d+\s*-\s*$'),
            re.compile(r'^\s*\d+\s*$'),
            re.compile(r'^\s*[-\s]+\d+[-\s]+$'),
        ]
        
        self.invalid_patterns = [
            re.compile(r'^\s*$'),
            re.compile(r'^\s*[=-_~*#*]+$'),
            re.compile(r'^\s*页码\s*\d*\s*$'),
        ]
    
    def _is_header(self, line: str) -> bool:
        """判断是否为页眉"""
        line = line.strip()
        if len(line) < 3 or len(line) > 100:
            return False
        for pattern in self.header_patterns:
            if pattern.match(line):
                return True
        return False
    
    def _is_footer(self, line: str) -> bool:
        """判断是否为页脚"""
        line = line.strip()
        if len(line) < 1 or len(line) > 50:
            return False
        for pattern in self.footer_patterns:
            if pattern.match(line):
                return True
        return False
    
    def _is_invalid(self, line: str) -> bool:
        """判断是否为无效内容"""
        line = line.strip()
        for pattern in self.invalid_patterns:
            if pattern.match(line):
                return True
        if len(line) == 1 and not line.isalnum():
            return True
        return False
    
    def _clean_line(self, line: str) -> str:
        """清洗单行文本"""
        line = line.strip()
        line = re.sub(r'\s+', ' ', line)
        line = line.replace('\r', '').replace('\n', ' ')
        return line
    
    def parse_pdf(self, file_path: str) -> Dict:
        """解析PDF文件"""
        if not HAS_PYPDF2:
            raise ImportError("PyPDF2未安装，请执行：uv add PyPDF2")
        
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在：{file_path}")
        
        result = {
            "file_name": os.path.basename(file_path),
            "file_path": file_path,
            "pages": [],
            "full_text": "",
            "metadata": {}
        }
        
        try:
            reader = PdfReader(file_path)
            
            if reader.metadata:
                result["metadata"] = {
                    "author": reader.metadata.author or "",
                    "title": reader.metadata.title or "",
                    "subject": reader.metadata.subject or "",
                }
            
            for page_num, page in enumerate(reader.pages, 1):
                page_text = page.extract_text()
                if not page_text:
                    continue
                
                lines = page_text.split('\n')
                cleaned_lines = []
                
                for line in lines:
                    line = self._clean_line(line)
                    if self._is_invalid(line):
                        continue
                    if self._is_header(line) or self._is_footer(line):
                        continue
                    if line:
                        cleaned_lines.append(line)
                
                page_content = '\n'.join(cleaned_lines)
                if page_content.strip():
                    result["pages"].append({
                        "page_num": page_num,
                        "content": page_content.strip()
                    })
            
            result["full_text"] = '\n\n'.join(p["content"] for p in result["pages"])
            
            print(f"✓ 解析PDF: {os.path.basename(file_path)} ({len(result['pages'])}页)")
        except Exception as e:
            print(f"✗ 解析PDF失败: {os.path.basename(file_path)} - {str(e)}")
        
        return result
    
    def parse_docx(self, file_path: str) -> Dict:
        """解析docx文件"""
        if not HAS_DOCX:
            raise ImportError("python-docx未安装，请执行：uv add python-docx")
        
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在：{file_path}")
        
        result = {
            "file_name": os.path.basename(file_path),
            "file_path": file_path,
            "paragraphs": [],
            "full_text": "",
            "metadata": {}
        }
        
        try:
            doc = Document(file_path)
            
            if doc.core_properties:
                result["metadata"] = {
                    "author": doc.core_properties.author or "",
                    "title": doc.core_properties.title or "",
                    "subject": doc.core_properties.subject or "",
                    "created": str(doc.core_properties.created) if doc.core_properties.created else "",
                }
            
            for para in doc.paragraphs:
                text = self._clean_line(para.text)
                if self._is_invalid(text):
                    continue
                if self._is_header(text) or self._is_footer(text):
                    continue
                if text:
                    style = para.style.name if para.style else "Normal"
                    result["paragraphs"].append({
                        "text": text,
                        "style": style
                    })
            
            result["full_text"] = '\n\n'.join(p["text"] for p in result["paragraphs"])
            
            print(f"✓ 解析DOCX: {os.path.basename(file_path)} ({len(result['paragraphs'])}段)")
        except Exception as e:
            print(f"✗ 解析DOCX失败: {os.path.basename(file_path)} - {str(e)}")
        
        return result
    
    def parse_file(self, file_path: str) -> Dict:
        """根据文件类型自动选择解析方法"""
        file_ext = os.path.splitext(file_path)[1].lower()
        
        if file_ext == '.pdf':
            return self.parse_pdf(file_path)
        elif file_ext in ['.docx', '.doc']:
            return self.parse_docx(file_path)
        else:
            raise ValueError(f"不支持的文件类型：{file_ext}")
    
    def batch_parse(self, directory: str, extensions: List[str] = None) -> List[Dict]:
        """批量解析目录中的文档"""
        if extensions is None:
            extensions = ['.pdf', '.docx']
        
        directory = Path(directory).resolve()
        if not directory.exists():
            raise FileNotFoundError(f"目录不存在：{directory}")
        
        results = []
        for ext in extensions:
            for file_path in directory.glob(f'**/*{ext}'):
                try:
                    result = self.parse_file(str(file_path))
                    if result.get("full_text", "").strip():
                        results.append(result)
                except Exception as e:
                    print(f"✗ 跳过文件: {file_path} - {str(e)}")
        
        print(f"\n批量解析完成，共解析 {len(results)} 个文件")
        return results
    
    def extract_sections(self, text: str, min_section_length: int = 50) -> List[Dict]:
        """从文本中提取章节结构"""
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
            
            if self._is_header(line) and len(line) < 80:
                if current_section["content"].strip():
                    if len(current_section["content"].strip()) >= min_section_length:
                        sections.append(current_section)
                current_section = {
                    "title": line,
                    "content": ""
                }
            else:
                current_section["content"] += line + '\n'
        
        if current_section["content"].strip():
            if len(current_section["content"].strip()) >= min_section_length:
                sections.append(current_section)
        
        return sections


def main():
    """批量解析示例"""
    parser = DocumentParser()
    
    documents_dir = Path("./datasets/documents")
    documents_dir.mkdir(exist_ok=True, parents=True)
    
    print("===== 文档解析工具 =====")
    print(f"扫描目录: {documents_dir.resolve()}")
    
    results = parser.batch_parse(str(documents_dir))
    
    output_dir = Path("./datasets/parsed_docs")
    output_dir.mkdir(exist_ok=True, parents=True)
    
    for result in results:
        output_path = output_dir / f"{os.path.splitext(result['file_name'])[0]}.txt"
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(result['full_text'])
        print(f"✓ 保存纯文本: {output_path}")
    
    print("\n✅ 文档解析完成！")


if __name__ == "__main__":
    main()