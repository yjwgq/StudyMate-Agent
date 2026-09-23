"""上传安全校验单测（M2-1，§13.7 / C6 / C7）。"""

import io
import zipfile

import pytest

from apps.api.core.upload_guard import (
    UploadRejected,
    check_suffix,
    kind_from_suffix,
    sanitize_filename,
    sniff_kind,
)


def _fake_pdf() -> bytes:
    return b"%PDF-1.7\n%%EOF\n" + b"\x00" * 16


def _fake_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w:p/>")
        zf.writestr("[Content_Types].xml", "<Types/>")
    return buf.getvalue()


def test_sniff_pdf_and_docx():
    assert sniff_kind(_fake_pdf()) == "pdf"
    assert sniff_kind(_fake_docx()) == "docx"


def test_sniff_html_case_insensitive():
    assert sniff_kind(b"  <!DOCTYPE html><html><body>x</body>") == "html"
    assert sniff_kind(b"\xef\xbb\xbf<html lang=\"zh\">") == "html"


def test_sniff_text():
    assert sniff_kind("# 标题\n中文 markdown 内容".encode()) == "text"


def test_c7_renamed_non_pdf_rejected():
    """C7：改后缀名的非 PDF 文件被 magic bytes 拦下。"""
    with pytest.raises(UploadRejected) as e:
        sniff_kind(b"PK\x03\x04not-a-word-doc")        # 任意 zip 改名 .pdf
    assert e.value.reason == "type_not_allowed"
    with pytest.raises(UploadRejected):
        sniff_kind(b"MZ\x90\x00binary-stub")           # PE 二进制
    with pytest.raises(UploadRejected):
        sniff_kind(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)  # PNG 伪装


def test_plain_zip_not_docx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "hi")
    with pytest.raises(UploadRejected):
        sniff_kind(buf.getvalue())


def test_check_suffix_whitelist():
    check_suffix("论文.pdf")
    check_suffix("notes.MD")
    with pytest.raises(UploadRejected):
        check_suffix("evil.exe")
    with pytest.raises(UploadRejected):
        check_suffix("noext")


def test_suffix_kind_consistency():
    """后缀声明与 magic 判定必须一致（C7：.pdf 后缀装文本 → 拒绝）。"""
    assert kind_from_suffix("a.pdf") == "pdf"
    assert kind_from_suffix("a.docx") == "docx"
    assert kind_from_suffix("a.md") == "text"
    # .pdf 后缀但内容是纯文本 → 两者不一致，路由层据此拒绝
    assert sniff_kind(b"plain text pretending to be pdf") == "text"
    assert kind_from_suffix("fake.pdf") == "pdf"
    assert kind_from_suffix("fake.pdf") != sniff_kind(b"plain text pretending to be pdf")


def test_sanitize_filename_blocks_traversal():
    assert "/" not in sanitize_filename("../../etc/passwd.pdf")
    assert "\\" not in sanitize_filename("..\\..\\win.ini.pdf")
    assert sanitize_filename("../../etc/passwd.pdf").endswith(".pdf")
    assert sanitize_filename("\x00\x01bad\x7fname.md") == "badname.md"
    assert sanitize_filename("   ") == "document"
    assert len(sanitize_filename("超长" * 100 + ".pdf")) <= 64
