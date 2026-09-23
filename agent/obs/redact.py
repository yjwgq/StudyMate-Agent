"""PII 脱敏（M3-4，§13.3）。

设计文档 v1.1 的强调点：trace 通道最容易漏 —— Langfuse SDK 默认把
input/output 全量上报。因此这里的规则是：

    任何要进 Langfuse / 日志的字符串，先过 mask_text；
    任何 dict / list 载荷，先过 mask_any（递归）。

覆盖范围（正则 + 运行时校验，无 NER 依赖，MVP 够用）：
    手机号 / 身份证 / 邮箱 / 常见密钥形态（sk- 前缀、Bearer token、JWT）。

验收 D4 依赖本模块 + 单元测试自动化断言：
    trace 载荷里出现测试手机号 = 脱敏失效 = 验收不过。
"""

import re
from typing import Any

# 手机号：1[3-9] 开头的 11 位数字（前后不能紧跟数字，避免切号）
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 身份证：18 位（末位可为 X），或 15 位旧证
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)|(?<!\d)\d{15}(?!\d)")
# 邮箱
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 密钥形态：sk- 前缀（OpenAI/DashScope/DeepSeek 风格）。
# 注意：Python 的 \b 把 CJK 也算 word 字符，「密钥sk-xxx」这种中英贴身
# 组合会匹配失败（单测覆盖）—— 边界用显式字符类，不用 \b
_SECRET_SK = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])")
# Bearer token（三段式 JWT 或任意长 token）
_BEARER = re.compile(r"(?<![A-Za-z0-9_-])Bearer\s+[A-Za-z0-9._-]{16,}(?![A-Za-z0-9_-])",
                     re.IGNORECASE)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"(?![A-Za-z0-9_-])"
)

MASK = "***REDACTED***"


def mask_text(text: str) -> str:
    """对单条字符串做 PII 脱敏。顺序：密钥 → 身份证 → 手机 → 邮箱。

    密钥/身份证在前：它们与手机号模式可能重叠（长数字串），
    先匹配更长的模式避免被手机号规则截断。
    """
    if not text:
        return text
    text = _SECRET_SK.sub(MASK, text)
    text = _BEARER.sub("Bearer " + MASK, text)
    text = _JWT.sub(MASK, text)
    text = _ID_CARD.sub(MASK, text)
    text = _PHONE.sub(MASK, text)
    text = _EMAIL.sub(MASK, text)
    return text


def mask_any(payload: Any) -> Any:
    """递归脱敏：dict / list / tuple 逐字段处理，其余标量转字符串后处理。

    Langfuse 的 input/output 是任意 JSON 载荷，必须在入口统一过这里；
    `mask=` 回调（agent/obs/langfuse.py）也用它兜底 SDK 内部序列化路径。
    """
    if payload is None or isinstance(payload, (bool, int, float)):
        return payload
    if isinstance(payload, str):
        return mask_text(payload)
    if isinstance(payload, dict):
        return {
            # key 也可能是 PII（少见但不为零成本），一并处理
            mask_text(str(k)) if isinstance(k, str) else k: mask_any(v)
            for k, v in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [mask_any(item) for item in payload]
    # bytes / 自定义对象等：转字符串脱敏，宁可多脱不可漏
    return mask_text(str(payload))
