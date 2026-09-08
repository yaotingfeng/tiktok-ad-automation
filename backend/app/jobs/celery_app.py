from celery import Celery  # type: ignore[import-untyped]
from kombu import Queue  # type: ignore[import-untyped]

from app.core.config import settings

celery_app = Celery("tiktok_ads", broker=settings.REDIS_URL)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    result_backend=None,
    imports=("app.jobs.tasks",),
    task_queues=(Queue("resources"), Queue("builds"), Queue("control")),
    task_default_queue="control",
    task_create_missing_queues=False,
    task_routes={"jobs.flush_dispatch": {"queue": "control"}},
    beat_schedule={
        "flush-dispatch": {
            "task": "jobs.flush_dispatch",
            "schedule": 5.0,
            "options": {"queue": "control"},
        }
    },
)
