"""Celery/Beat adapters for scheduled AI build occurrences."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.tasks import register_dispatch_task
from app.modules.ai_workbench.build_handoff import (
    OCCURRENCE_NAMESPACE,
    TASK_NAME,
    BuildHandoff,
    _scheduled_at,
)
from app.modules.ai_workbench.models import AIOptimizer
from app.modules.ai_workbench.plans import AIDailyPlanRow

register_dispatch_task(TASK_NAME, "builds")
PLAN_DISPATCH_TASK = "ai_workbench.dispatch_due_plans"
register_dispatch_task(PLAN_DISPATCH_TASK, "control")


def dispatch_occurrence_task(
    *, database_engine: Any, tenant_id: UUID, actor_id: UUID, payload: dict[str, Any]
) -> None:
    if set(payload) != {"occurrence_id"}:
        raise DomainError("dispatch_payload_invalid", "AI 搭建发生记录投递参数无效")
    try:
        occurrence_id = UUID(payload["occurrence_id"])
    except (TypeError, ValueError, AttributeError):
        raise DomainError("dispatch_payload_invalid", "AI 搭建发生记录标识无效") from None
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    with Session(database_engine) as session, session.begin():
        BuildHandoff(session, context=context).dispatch_occurrence(occurrence_id)


def dispatch_due_plans(*, database_engine: Any = engine, limit: int = 100) -> int:
    """Claim due rows with tenant-safe locks and create one occurrence each.

    Unknown occurrences are intentionally ignored by the handoff's terminal
    guard; a Beat retry therefore records no second platform submission.
    """
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    now = datetime.now(UTC)
    created = 0
    with Session(database_engine) as session, session.begin():
        rows = session.exec(
            select(AIDailyPlanRow)
            .order_by(col(AIDailyPlanRow.business_date), col(AIDailyPlanRow.scheduled_time), col(AIDailyPlanRow.id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            scheduled_at = _scheduled_at(row)
            optimizer = session.exec(
                select(AIOptimizer).where(
                    AIOptimizer.tenant_id == row.tenant_id,
                    AIOptimizer.id == row.optimizer_id,
                )
            ).one_or_none()
            if optimizer is None or not optimizer.active:
                if optimizer is not None:
                    context = TenantContext(tenant_id=row.tenant_id, actor_id=optimizer.created_by, role="operator")
                    BuildHandoff(session, context=context).create_occurrence(
                        row.id,
                        uuid5(OCCURRENCE_NAMESPACE, f"skip:{row.id}:inactive"),
                        enqueue=False,
                        skip_reason="ai_optimizer_inactive",
                        allow_inactive=True,
                        allow_missing_provider=True,
                    )
                continue
            actor_id = optimizer.created_by
            request_id = uuid5(
                OCCURRENCE_NAMESPACE,
                f"scheduled:{row.id}:{scheduled_at.isoformat()}:{optimizer.latest_version}",
            )
            context = TenantContext(tenant_id=row.tenant_id, actor_id=actor_id, role="operator")
            handoff = BuildHandoff(session, context=context)
            if scheduled_at > now:
                occurrence = handoff.create_occurrence(
                    row.id,
                    request_id,
                    enqueue=False,
                    skip_reason="future",
                )
                occurrence.evidence = {**occurrence.evidence, "skip_reason": "future"}
                session.add(occurrence)
                continue
            try:
                occurrence = handoff.create_occurrence(row.id, request_id)
            except DomainError as error:
                # Keep a durable blocked occurrence when provider/application
                # readiness changes after the plan was saved.
                occurrence = handoff.create_occurrence(
                    row.id,
                    request_id,
                    enqueue=False,
                    skip_reason=error.code,
                    allow_inactive=True,
                    allow_missing_provider=True,
                )
            # The occurrence ledger is the claim.  Existing UNKNOWN rows are
            # returned by create_occurrence and never submitted again.
            if occurrence.status == "PENDING":
                handoff.dispatch_occurrence(occurrence.id)
            elif occurrence.status == "WAITING_MATERIAL":
                handoff.schedule_retry(occurrence.id)
            created += 1
    return created


@celery_app.task(
    bind=True,
    name=TASK_NAME,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)  # type: ignore[untyped-decorator]
def dispatch_occurrence_celery(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    del self
    dispatch_occurrence_task(
        database_engine=engine,
        tenant_id=UUID(tenant_id),
        actor_id=UUID(actor_id),
        payload=payload,
    )


@celery_app.task(name=PLAN_DISPATCH_TASK, time_limit=60, soft_time_limit=55)  # type: ignore[untyped-decorator]
def dispatch_due_plans_task() -> int:
    return dispatch_due_plans(database_engine=engine)

