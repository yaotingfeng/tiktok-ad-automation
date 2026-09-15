"""源封面启动事件不执行网络；实际图片操作仍由封面 Worker 执行。"""

from typing import Any
from uuid import UUID

from sqlmodel import Session

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app

from .source_cover_service import prepare_source_cover, repair_source_cover_starts
from .tasks import require_bounded_worker


@celery_app.task(  # type: ignore[untyped-decorator]
    name="materials.prepare_source_cover", bind=True, time_limit=45, soft_time_limit=40
)
def prepare_source_cover_task(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    require_bounded_worker(self, hard_limit=45)
    try:
        if set(payload) != {"operation_id"}:
            raise ValueError
        context = TenantContext(
            tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
        )
        operation_id, dispatch_id = UUID(payload["operation_id"]), UUID(self.request.id)
    except ValueError, TypeError, AttributeError:
        raise DomainError("invalid_asset_task", "源封面任务参数无效") from None
    with Session(engine) as session, session.begin():
        prepare_source_cover(
            session, context=context, operation_id=operation_id, dispatch_id=dispatch_id
        )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="materials.repair_source_cover_starts", time_limit=45, soft_time_limit=40
)
def repair_source_cover_starts_task() -> int:
    with Session(engine) as session, session.begin():
        return repair_source_cover_starts(session)
