"""Read-only reconciliation for management items with unknown provider effect."""

from uuid import UUID

from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.ad_management.execution import _public
from app.modules.ad_management.models import ManagementTask, ManagementTaskItem
from app.modules.ad_management.schemas import ManagementItemPublic


def reconcile_item(session: Session, context: TenantContext, item_id: UUID) -> ManagementItemPublic:
    item = session.exec(
        select(ManagementTaskItem).where(
            ManagementTaskItem.id == item_id,
            ManagementTaskItem.tenant_id == context.tenant_id,
        )
    ).one_or_none()
    if item is None:
        raise DomainError("management_item_not_found", "管理任务项不存在")
    task = session.exec(
        select(ManagementTask).where(
            ManagementTask.id == item.task_id,
            ManagementTask.tenant_id == context.tenant_id,
            ManagementTask.actor_id == context.actor_id,
        )
    ).one_or_none()
    if task is None:
        raise DomainError("action_forbidden", "管理任务操作者不匹配")
    # No provider readback is guessed here.  This endpoint only records that a
    # human requested evidence review; UNKNOWN remains terminal and cannot be
    # converted into a resend by a repeated queue delivery.
    if item.execution_result == "UNKNOWN":
        item.observation_state = "RECONCILE_REQUESTED"
        session.add(item)
        session.flush()
    return _public(item)


__all__ = ["reconcile_item"]
