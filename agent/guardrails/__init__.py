"""安全护栏 —— 由 M8 / M9 分阶段落地。

    injection.py   提示注入检测（规则 + LLM 双层）
    pii.py         PII 脱敏：日志 / 模型 / **Langfuse trace** 三通道
    moderation.py  内容审核
    groundedness.py 逐句事实核验

注意：trace 通道最容易漏脱敏 —— SDK 默认会上报完整的 input/output。
"""
