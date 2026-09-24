"""Celery 异步任务 —— 由 M2 / M9 落地。

    celery_app.py        应用与队列定义
    tasks/ingest.py      文档入库（独立 ingest 队列，避免大文件阻塞提醒）
    tasks/briefing.py    每日简报（幂等：UNIQUE(user_id, deliver_date)）
    tasks/reminder.py    到期提醒（幂等：reminded_at）
    tasks/consolidation.py 记忆巩固（水印增量扫描）

队列分级：default（简报/提醒/巩固）与 ingest（入库）必须分开，
否则一个用户上传大文件会阻塞所有人的提醒（ADR-5）。
"""
