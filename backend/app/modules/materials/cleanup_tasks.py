"""Bounded maintenance workers; actor deactivation does not undo stored receipts."""

from typing import Any
from uuid import UUID

from sqlmodel import Session

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch

from .cleanup import CLEANUP_HARD_LIMIT, repair_cleanups, run_cleanup
from .tasks import require_bounded_worker


@celery_app.task(
    name="materials.cleanup_original",
    bind=True,
    time_limit=CLEANUP_HARD_LIMIT,
    soft_time_limit=CLEANUP_HARD_LIMIT - 10,
)  # type: ignore[untyped-decorator]
def cleanup_original(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    require_bounded_worker(self, hard_limit=CLEANUP_HARD_LIMIT)
    try:
        if (
            set(payload) != {"cleanup_id", "generation"}
            or type(payload["generation"]) is not int
            or payload["generation"] < 1
        ):
            raise ValueError
        tenant, actor, identity, dispatch_id = (
            UUID(tenant_id),
            UUID(actor_id),
            UUID(payload["cleanup_id"]),
            UUID(self.request.id),
        )
    except ValueError, TypeError, KeyError, AttributeError:
        raise DomainError("invalid_asset_task", "清理任务参数无效") from None
    with Session(engine) as session:
        dispatch = session.get(PendingDispatch, dispatch_id)
        if not dispatch or (
            dispatch.tenant_id,
            dispatch.actor_id,
            dispatch.task_name,
            dispatch.payload,
        ) != (tenant, actor, "materials.cleanup_original", payload):
            raise DomainError("invalid_asset_task", "清理任务身份不一致")
    run_cleanup(
        database_engine=engine,
        cleanup_id=identity,
        tenant_id=tenant,
        generation=payload["generation"],
    )


@celery_app.task(name="materials.repair_cleanups", time_limit=45, soft_time_limit=40)  # type: ignore[untyped-decorator]
def repair_cleanup_tasks(limit: int = 100) -> int:
    with Session(engine) as session, session.begin():
        return repair_cleanups(session, limit=limit)
