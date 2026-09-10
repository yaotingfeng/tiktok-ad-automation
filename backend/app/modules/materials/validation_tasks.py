"""Only prefork workers may stream untrusted originals or run media inspection."""

from typing import Any
from uuid import UUID

from sqlmodel import Session

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app

from .object_validation import (
    VALIDATION_HARD_LIMIT,
    repair_validations,
    validate_original,
)
from .tasks import require_bounded_worker


@celery_app.task(
    name="materials.validate_original",
    bind=True,
    time_limit=VALIDATION_HARD_LIMIT,
    soft_time_limit=VALIDATION_HARD_LIMIT - 10,
)  # type: ignore[untyped-decorator]
def validate_original_task(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    require_bounded_worker(self, hard_limit=VALIDATION_HARD_LIMIT)
    try:
        if (
            set(payload) != {"object_id", "generation", "revision"}
            or type(payload["generation"]) is not int
            or type(payload["revision"]) is not int
            or min(payload["generation"], payload["revision"]) < 1
        ):
            raise ValueError
        context = TenantContext(
            tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
        )
        object_id, dispatch_id = UUID(payload["object_id"]), UUID(self.request.id)
    except ValueError, TypeError, KeyError, AttributeError:
        raise DomainError("invalid_asset_task", "原件校验任务参数无效") from None
    validate_original(
        database_engine=engine,
        context=context,
        object_id=object_id,
        dispatch_id=dispatch_id,
        generation=payload["generation"],
        revision=payload["revision"],
    )


@celery_app.task(name="materials.repair_validations", time_limit=45, soft_time_limit=40)  # type: ignore[untyped-decorator]
def repair_validation_tasks(limit: int = 100) -> int:
    with Session(engine) as session, session.begin():
        return repair_validations(session, limit=limit)
