"""Bounded real capability consumers; no external request is made by Beat."""

from typing import Any
from uuid import UUID

from redis import Redis

from app.core.config import settings
from app.core.db import engine
from app.jobs.celery_app import celery_app
from app.modules.accounts.capabilities import (
    TASK_NAME,
    process_capability,
    repair_capabilities,
)


@celery_app.task(
    name=TASK_NAME,
    time_limit=45,
    soft_time_limit=40,
    acks_late=True,
    reject_on_worker_lost=True,
)  # type: ignore[untyped-decorator]
def refresh_capabilities_task(
    *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    with Redis.from_url(settings.REDIS_URL, decode_responses=True) as redis_client:
        process_capability(
            database_engine=engine,
            redis_client=redis_client,
            tenant_id=UUID(tenant_id),
            actor_id=UUID(actor_id),
            payload=payload,
        )


@celery_app.task(name="accounts.repair_capabilities", time_limit=45, soft_time_limit=40)  # type: ignore[untyped-decorator]
def repair_capabilities_task() -> int:
    return repair_capabilities(database_engine=engine)
