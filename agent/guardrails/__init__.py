"""安全护栏 —— 分阶段落地。

    groundedness.py  逐句事实核验（M3，§8.4，验收 D1）
    injection.py     提示注入检测（M9）
    pii.py           → 已由 agent/obs/redact.py 承担（M3，日志/trace 双通道）
    moderation.py    内容审核（M9）

注意：trace 通道最容易漏脱敏 —— SDK 默认会上报完整的 input/output，
任何进 Langfuse 的载荷必须过 agent/obs/redact.py。
"""
