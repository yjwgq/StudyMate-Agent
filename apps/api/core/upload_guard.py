"""上传安全校验（M2-1，§13.7 上传层）。

四件事，全部在把文件交给下游之前完成：
    1. 大小限制：流式累计字节数，超上限立即拒绝（不信任 Content-Length）；
    2. MIME + magic bytes 双重校验：后缀/mime 声明什么不重要，
       以文件头判定真实类型（C7：改后缀名的非 PDF 必须被拦下）；
       zip 容器还要求内部含 word/document.xml 才认作 DOCX，
       防止任意 zip 改名 .docx 混入；
    3. 文件名净化：只取 basename、去控制符、限长 —— 存储文件名最终用
       document_id，净化后的名字仅作 title / 展示；
    4. 配额：每用户文档数上限（在路由层查库执行）。

纯函数、无 IO，方便单元测试。
"""

import re

# 声明层允许的扩展名（.html/.htm → html；.md/.txt → text）
ALLOWED_SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".html", ".htm"}

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_HTML_HEAD_RE = re.compile(
    rb"^(?:\xef\xbb\xbf)?\s*(?:<!doctype\s+html|<html)", re.IGNORECASE
)


class UploadRejected(Exception):
    """上传被拒绝；message 面向用户。"""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason  # 机器可读原因：too_large / type_not_allowed / fake_extension


def sniff_kind(data: bytes) -> str:
    """按文件头判定真实类型：pdf / docx / html / text；不认识则抛 UploadRejected。"""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        import io
        import zipfile

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if "word/document.xml" in zf.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            pass
        raise UploadRejected(
            "文件内容不是支持的文档类型", reason="type_not_allowed"
        )
    if _HTML_HEAD_RE.match(data):
        return "html"
    try:
        data.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        raise UploadRejected(  # noqa: TRY301 —— 保持调用方拿到统一异常
            "无法识别的文件类型（仅支持 PDF / DOCX / MD / TXT / HTML）",
            reason="type_not_allowed",
        ) from None


_SUFFIX_KIND = {".pdf": "pdf", ".docx": "docx", ".html": "html", ".htm": "html",
                ".md": "text", ".txt": "text"}


def kind_from_suffix(original_name: str) -> str | None:
    """由扩展名推定「声明」的类型；无白名单后缀返回 None（由 check_suffix 拦）。"""
    name = original_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    suffix = name[dot:].lower() if dot >= 0 else ""
    return _SUFFIX_KIND.get(suffix)


def check_suffix(original_name: str) -> None:
    """扩展名白名单（与 magic 校验互补：双保险）。"""
    name = original_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    suffix = name[dot:].lower() if dot >= 0 else ""
    if suffix not in ALLOWED_SUFFIXES:
        raise UploadRejected(
            f"不支持的文件扩展名：{suffix or '(无)'}", reason="type_not_allowed"
        )


def sanitize_filename(name: str, max_len: int = 64) -> str:
    """净化原始文件名：去路径成分、去控制符、限长。仅用于展示/标题。"""
    name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = _CONTROL_CHARS_RE.sub("", name).strip().strip(".")
    if len(name) > max_len:
        dot = name.rfind(".")
        stem, suffix = (name[:max_len], "") if dot < 0 else (
            name[: max_len - len(name[dot:])], name[dot:]
        )
        name = stem + suffix
    return name or "document"
