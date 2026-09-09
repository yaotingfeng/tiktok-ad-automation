"""Prefork-only 45-second cover workers; each dispatch is revision fenced."""

from typing import Any
from uuid import UUID

from redis import Redis
from sqlmodel import Session

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app

from .covers import HARD_LIMIT, SOFT_LIMIT, repair_cover_dispatches, run_cover
from .tasks import require_bounded_worker


def _run(
    task: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any], read: bool
) -> None:
    require_bounded_worker(task, hard_limit=HARD_LIMIT)
    try:
        if (
            set(payload) != {"job_id", "revision"}
            or type(payload["revision"]) is not int
            or payload["revision"] < 1
        ):
            raise ValueError
        context = TenantContext(
            tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
        )
        identity, dispatch = UUID(payload["job_id"]), UUID(task.request.id)
    except ValueError, TypeError, KeyError, AttributeError:
        raise DomainError("invalid_asset_task", "封面任务参数无效") from None
    with Redis.from_url(settings.REDIS_URL) as redis_client:
        run_cover(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            job_id=identity,
            dispatch_id=dispatch,
            revision=payload["revision"],
            read=read,
        )


@celery_app.task(
    name="materials.prepare_cover",
    bind=True,
    time_limit=HARD_LIMIT,
    soft_time_limit=SOFT_LIMIT,
)  # type: ignore[untyped-decorator]
def prepare_cover(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    _run(self, tenant_id=tenant_id, actor_id=actor_id, payload=payload, read=False)


@celery_app.task(
    name="materials.verify_cover",
    bind=True,
    time_limit=HARD_LIMIT,
    soft_time_limit=SOFT_LIMIT,
)  # type: ignore[untyped-decorator]
def verify_cover(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    _run(self, tenant_id=tenant_id, actor_id=actor_id, payload=payload, read=True)


@celery_app.task(
    name="materials.repair_covers", time_limit=HARD_LIMIT, soft_time_limit=SOFT_LIMIT
)  # type: ignore[untyped-decorator]
def repair_covers(limit: int = 100) -> int:
    with Session(engine) as session, session.begin():
        return repair_cover_dispatches(session, limit=limit)
