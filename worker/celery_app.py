"""Celery 应用（M2-8，§8.1：ingest 走独立队列）。

- broker/backend 复用 Redis（M9 起再按队列分级拆 worker）；
- 任务用名字注册（include worker.tasks.ingest），API 侧用
  celery_app.send_task("worker.tasks.ingest.ingest_document") 投递，
  避免接入层反向依赖 worker 的实现；
- 序列化固定 JSON（pickle 在多版本代码共存时是坑）。
"""

from celery import Celery

from apps.api.core.config import settings

celery_app = Celery(
    "personal-agent-os",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["worker.tasks.ingest"],
)

celery_app.conf.update(
    task_default_queue="ingest",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # ingest 是重活（解析 + embedding），单 worker 并发 2 足够；
    # 大文件卡死由任务内 time_limit 兜底
    task_soft_time_limit=600,
    task_time_limit=660,
    worker_prefetch_multiplier=1,   # 长任务公平分发
    broker_connection_retry_on_startup=True,
)
