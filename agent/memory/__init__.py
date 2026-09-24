"""三级记忆体系 —— 由 M8 落地。

    buffer.py          短期缓冲（Redis，近 K=10 轮）
    episodic.py        情景记忆（被提炼过的对话片段）
    semantic.py        语义记忆（画像 / 偏好 / 事实 / 指令）
    consolidation.py   增量巩固（水印机制）与冲突消解
    safety.py          记忆安全五层：来源隔离 / 类型管控 / 内容审核 / 注入降权 / 可溯源

安全要点：记忆是**持久化的信任边界** —— 一条被写入的 instruction
会永久注入 system prompt，绕过所有单轮输入护栏（§13.9）。
"""
