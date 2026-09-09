"""Local bounded recovery dispatch; provider I/O remains in original handlers."""

from typing import Any
from uuid import UUID

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.modules.builds.recovery import TASK_NAME, process_recovery, repair_recoveries


@celery_app.task(
    name=TASK_NAME,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)  # type: ignore[untyped-decorator]
def recover_submission_task(
    *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    try:
        context = TenantContext(
            tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
        )
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "恢复任务操作者无效") from None
    process_recovery(database_engine=engine, context=context, payload=payload)


@celery_app.task(name="builds.repair_recoveries", time_limit=60, soft_time_limit=55)  # type: ignore[untyped-decorator]
def repair_recoveries_task() -> int:
    return repair_recoveries(database_engine=engine)
