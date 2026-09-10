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
    imports=(
        "app.jobs.tasks",
        "app.modules.accounts.tasks",
        "app.modules.accounts.capability_tasks",
        "app.modules.providers.tasks",
        "app.modules.materials.tasks",
        "app.modules.materials.cover_tasks",
        "app.modules.materials.validation_tasks",
        "app.modules.materials.cleanup_tasks",
        "app.modules.materials.ingest_tasks",
        "app.modules.builds.draft_tasks",
        "app.modules.builds.preview_tasks",
        "app.modules.builds.submission_tasks",
        "app.modules.builds.tasks",
        "app.modules.builds.scene_tasks",
        "app.modules.builds.recovery_tasks",
    ),
    task_queues=(Queue("resources"), Queue("builds"), Queue("control")),
    task_default_queue="control",
    task_create_missing_queues=False,
    task_routes={"jobs.flush_dispatch": {"queue": "control"}},
    beat_schedule={
        "scan-abandoned-materials": {
            "task": "materials.scan_abandoned_objects",
            "schedule": 30.0,
            "options": {"queue": "control"},
        },
        "repair-material-ingest-transports": {
            "task": "materials.repair_ingest_transports",
            "schedule": 30.0,
            "options": {"queue": "control"},
        },
        "repair-material-cleanups": {
            "task": "materials.repair_cleanups",
            "schedule": 30.0,
            "options": {"queue": "control"},
        },
        "repair-material-validations": {
            "task": "materials.repair_validations",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-material-covers": {
            "task": "materials.repair_covers",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-build-recoveries": {
            "task": "builds.repair_recoveries",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-build-scenes": {
            "task": "builds.repair_scenes",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-build-execution": {
            "task": "builds.repair_execution",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-build-submissions": {
            "task": "builds.repair_submissions",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-account-capabilities": {
            "task": "accounts.repair_capabilities",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-build-previews": {
            "task": "builds.repair_previews",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-material-dispatches": {
            "task": "materials.repair_dispatches",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-draft-preparations": {
            "task": "builds.repair_draft_preparations",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "recover-provider-preparations": {
            "task": "providers.recover_preparations",
            "schedule": 15.0,
            "options": {"queue": "control"},
        },
        "flush-dispatch": {
            "task": "jobs.flush_dispatch",
            "schedule": 1.0,
            "options": {"queue": "control"},
        },
    },
)
