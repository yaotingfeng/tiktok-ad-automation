"""Celery handoff and single-slot management worker."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.outbox import validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.ad_management.execution import execute_item
from app.modules.ad_management.models import ManagementTask, ManagementTaskItem

MANAGEMENT_DISPATCH_TASK = "ad_management.execute"
register_dispatch_task(MANAGEMENT_DISPATCH_TASK, "ad-management")


def _payload(payload: dict[str, Any]) -> tuple[UUID, int]:
    validate_dispatch_payload(payload)
    if set(payload) != {"task_id", "generation"}:
        raise DomainError("dispatch_payload_invalid", "管理任务参数无效")
    try:
        task_id = UUID(str(payload["task_id"]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "管理任务标识无效") from exc
    generation = payload["generation"]
    if type(generation) is not int or generation < 1:
        raise DomainError("dispatch_payload_invalid", "管理任务领取代数无效")
    return task_id, generation


@celery_app.task(name=MANAGEMENT_DISPATCH_TASK)
def execute_management_task(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> str:
    try:
        tenant = UUID(tenant_id)
        actor = UUID(actor_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "管理任务操作者无效") from exc
    task_id, _dispatch_generation = _payload(payload)
    with Session(engine) as session:
        task = session.exec(
            select(ManagementTask).where(
                ManagementTask.id == task_id,
                ManagementTask.tenant_id == tenant,
                ManagementTask.actor_id == actor,
            )
        ).one_or_none()
        if task is None:
            raise DomainError("resource_not_found", "管理任务不存在")
        refresh_pending = session.exec(
            select(ManagementTaskItem.id).where(
                ManagementTaskItem.task_id == task_id,
                ManagementTaskItem.tenant_id == tenant,
                ManagementTaskItem.execution_result == "ACCEPTED",
                ManagementTaskItem.observation_state == "REFRESH_PENDING",
            )
        ).first()
        if task.status in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "NEEDS_REVIEW"} and refresh_pending is None:
            return task.status
        task.status = "RUNNING"
        session.add(task)
        session.commit()
        item_ids = [
            row.id
            for row in session.exec(
                select(ManagementTaskItem)
                .where(
                    ManagementTaskItem.task_id == task_id,
                    ManagementTaskItem.tenant_id == tenant,
                )
                .order_by(col(ManagementTaskItem.position))
            ).all()
        ]
    for item_id in item_ids:
        try:
            execute_item(engine, item_id)
        except Exception:
            # One malformed, revoked, or fenced item must not block siblings.
            continue
    with Session(engine) as session:
        task = session.get(ManagementTask, task_id)
        if task is None:
            return "FAILED"
        rows = session.exec(select(ManagementTaskItem).where(ManagementTaskItem.task_id == task_id, ManagementTaskItem.tenant_id == tenant)).all()
        statuses = {row.execution_result for row in rows}
        if "UNKNOWN" in statuses:
            task.status = "NEEDS_REVIEW"
        elif "PENDING" in statuses or "NOT_SENT" in statuses:
            task.status = "RUNNING"
        elif statuses and statuses <= {"ACCEPTED", "NO_CHANGE"}:
            task.status = "SUCCEEDED"
        else:
            task.status = "PARTIAL"
        session.add(task)
        session.commit()
        return task.status


__all__ = ["MANAGEMENT_DISPATCH_TASK", "execute_management_task"]
