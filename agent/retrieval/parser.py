"""文档解析（M2-3，§8.1）。

MVP 解析路由（范围裁剪后只用 PyMuPDF，见实施计划 §9 第 5 条）：
    PDF   → PyMuPDF（fitz）
    DOCX  → PyMuPDF，异常时回退标准库（docx 即 zip + XML：抽取 w:p / w:t）
    HTML  → PyMuPDF，异常时回退正则去标签
    MD/TXT→ 纯文本（单页）

安全（§13.7 解析层）：
    - zip 解压比预检：DOCX 解压后总量超上限 → 拒绝（防 zip bomb）；
    - 页数上限：超限拒绝（防巨型文档拖垮 worker）；
    - 解析本身运行在 Celery worker 子进程，与 API 进程隔离（compose 层保证）。
"""

import io
import re
import xml.etree.ElementTree as ET  # noqa: S405 —— 输入为用户文档，本模块只提取文本且上游有大小/解压比限制
import zipfile
from dataclasses import dataclass, field

import fitz  # PyMuPDF

SUPPORTED_KINDS = ("pdf", "docx", "html", "text")


class ParseError(Exception):
    """解析失败（调用方据此把文档标记 failed / 拒绝上传）。"""


@dataclass
class ParsedPage:
    page_no: int   # 1-based
    text: str


@dataclass
class ParsedDoc:
    pages: list[ParsedPage]
    parser_used: str
    meta: dict = field(default_factory=dict)


def _check_zip_ratio(data: bytes, max_unzip_mb: int) -> None:
    """DOCX 是 zip：解压后总量超限视为 zip bomb，直接拒绝。"""
    limit = max_unzip_mb * 1024 * 1024
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            total = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile as exc:
        raise ParseError(f"DOCX 不是合法的 zip 容器：{exc}") from exc
    if total > limit:
        raise ParseError(
            f"DOCX 解压后总量 {total / 1048576:.0f}MB 超过上限 {max_unzip_mb}MB（疑似 zip bomb）"
        )


def _parse_pdf(data: bytes, max_pages: int) -> ParsedDoc:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 —— fitz 抛的类型不稳定，统一转 ParseError
        raise ParseError(f"PDF 打开失败：{exc}") from exc
    if doc.page_count > max_pages:
        raise ParseError(f"PDF 页数 {doc.page_count} 超过上限 {max_pages}")
    pages = [
        ParsedPage(page_no=i + 1, text=doc[i].get_text("text"))
        for i in range(doc.page_count)
    ]
    doc.close()
    return ParsedDoc(pages=pages, parser_used="pymupdf", meta={"kind": "pdf"})


def _docx_fallback(data: bytes) -> ParsedDoc:
    """标准库兜底：解压 word/document.xml，按 <w:p> 段落、<w:t> 文本提取。"""
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    pages: list[ParsedPage] = []
    para_lines: list[str] = []
    for p in root.iter(f"{ns}p"):
        text = "".join(t.text or "" for t in p.iter(f"{ns}t")).strip()
        if text:
            para_lines.append(text)
    pages.append(ParsedPage(page_no=1, text="\n".join(para_lines)))
    return ParsedDoc(pages=pages, parser_used="docx-xml", meta={"kind": "docx"})


def _parse_docx(data: bytes, max_pages: int, max_unzip_mb: int) -> ParsedDoc:
    _check_zip_ratio(data, max_unzip_mb)
    try:
        doc = fitz.open(stream=data, filetype="docx")
        pages = [
            ParsedPage(page_no=i + 1, text=doc[i].get_text("text"))
            for i in range(doc.page_count)
        ]
        doc.close()
        if not any(p.text.strip() for p in pages):
            raise ParseError("DOCX 解析结果为空")
        return ParsedDoc(pages=pages, parser_used="pymupdf", meta={"kind": "docx"})
    except ParseError:
        raise
    except Exception:
        # PyMuPDF 对部分 docx 变体支持不全 → 标准库 XML 兜底
        return _docx_fallback(data)


_TAG_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BARE_TAG_RE = re.compile(r"<[^>]+>")


def _html_fallback(data: bytes) -> ParsedDoc:
    text = data.decode("utf-8", errors="replace")
    text = _TAG_RE.sub(" ", text)
    text = _BARE_TAG_RE.sub("\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return ParsedDoc(pages=[ParsedPage(page_no=1, text=text)], parser_used="html-regex",
                     meta={"kind": "html"})


def _parse_html(data: bytes, max_pages: int) -> ParsedDoc:
    try:
        doc = fitz.open(stream=data, filetype="html")
        pages = [
            ParsedPage(page_no=i + 1, text=doc[i].get_text("text"))
            for i in range(doc.page_count)
        ]
        doc.close()
        if pages and any(p.text.strip() for p in pages):
            return ParsedDoc(pages=pages, parser_used="pymupdf", meta={"kind": "html"})
    except Exception:  # noqa: BLE001 —— 统一回退
        pass
    return _html_fallback(data)


def _parse_text(data: bytes) -> ParsedDoc:
    text = data.decode("utf-8", errors="replace")
    return ParsedDoc(pages=[ParsedPage(page_no=1, text=text)], parser_used="plaintext",
                     meta={"kind": "text"})


def parse_bytes(
    data: bytes,
    kind: str,
    *,
    max_pages: int = 2000,
    max_unzip_mb: int = 500,
) -> ParsedDoc:
    """按 magic bytes 判定出的 kind 解析文档字节流。

    kind ∈ {"pdf","docx","html","text"}（由 upload 层 sniff 得出，C7 验收点）。
    """
    if kind == "pdf":
        return _parse_pdf(data, max_pages)
    if kind == "docx":
        return _parse_docx(data, max_pages, max_unzip_mb)
    if kind == "html":
        return _parse_html(data, max_pages)
    if kind == "text":
        return _parse_text(data)
    raise ParseError(f"不支持的文档类型：{kind}")
