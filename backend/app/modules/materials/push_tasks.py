"""外部原件只在有硬时限的 prefork worker 校验。"""

from typing import Any
from uuid import UUID

from sqlmodel import Session

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.tasks import register_dispatch_task

from .push_worker import IMPORT_HARD_LIMIT, process_item, repair_imports
from .tasks import require_bounded_worker

register_dispatch_task("materials.import_external", "resources")


@celery_app.task(
    name="materials.import_external",
    bind=True,
    time_limit=IMPORT_HARD_LIMIT,
    soft_time_limit=IMPORT_HARD_LIMIT - 10,
)  # type: ignore[untyped-decorator]
def import_external_task(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    require_bounded_worker(self, hard_limit=IMPORT_HARD_LIMIT)
    try:
        if set(payload) != {"item_id"}:
            raise ValueError
        context = TenantContext(UUID(tenant_id), UUID(actor_id), "operator")
        item_id = UUID(payload["item_id"])
    except ValueError, TypeError, KeyError:
        raise DomainError("invalid_asset_task", "外部素材任务参数无效") from None
    process_item(database_engine=engine, context=context, item_id=item_id)


@celery_app.task(
    name="materials.repair_external_imports", time_limit=45, soft_time_limit=40
)  # type: ignore[untyped-decorator]
def repair_external_imports_task(limit: int = 100) -> int:
    with Session(engine) as session, session.begin():
        return repair_imports(session, limit=limit)
