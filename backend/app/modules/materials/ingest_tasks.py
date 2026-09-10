"""Bounded recovery workers for interrupted multipart control-plane requests."""

from typing import Any
from uuid import UUID

from sqlmodel import Session

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app

from .ingest_transport import (
    TRANSPORT_HARD_LIMIT,
    reconcile_ingest_transport,
    repair_ingest_transports,
)
from .tasks import require_bounded_worker


@celery_app.task(
    name="materials.reconcile_ingest_transport",
    bind=True,
    time_limit=TRANSPORT_HARD_LIMIT,
    soft_time_limit=TRANSPORT_HARD_LIMIT - 10,
)  # type: ignore[untyped-decorator]
def reconcile_ingest_transport_task(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    require_bounded_worker(self, hard_limit=TRANSPORT_HARD_LIMIT)
    try:
        if set(payload) != {"object_id", "generation", "revision"} or any(
            type(payload[key]) is not int or payload[key] < 1
            for key in ("generation", "revision")
        ):
            raise ValueError
        context = TenantContext(
            tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
        )
        object_id = UUID(payload["object_id"])
    except ValueError, TypeError, KeyError:
        raise DomainError("invalid_asset_task", "素材恢复任务参数无效") from None
    reconcile_ingest_transport(
        database_engine=engine,
        context=context,
        object_id=object_id,
        generation=payload["generation"],
        revision=payload["revision"],
    )


@celery_app.task(
    name="materials.repair_ingest_transports",
    bind=True,
    time_limit=45,
    soft_time_limit=40,
)  # type: ignore[untyped-decorator]
def repair_ingest_transports_task(self: Any, limit: int = 100) -> int:
    require_bounded_worker(self, hard_limit=45)
    with Session(engine) as db, db.begin():
        return repair_ingest_transports(db, limit=limit)
