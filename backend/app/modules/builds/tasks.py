"""Celery delivery adapters; all continuations are committed through the outbox."""

from datetime import UTC, datetime
from math import ceil
from typing import Any
from uuid import UUID

from redis import Redis
from sqlmodel import Session, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.modules.builds.dispatch import (
    READ_TASK,
    STEP_TASK,
    UNIT_TASK,
    finalize_submission,
    payload_identity,
    process_unit,
    queue_step,
    repair_execution,
    wake_unit,
)
from app.modules.builds.execution import process_step
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_state import evidence
from app.modules.builds.reconciliation import process_reconciliation
from app.modules.materials.models import MaterialDistribution


def _scope(tenant_id: str, actor_id: str) -> TenantContext:
    try:
        return TenantContext(
            tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
        )
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "任务操作者无效") from None


def finish_delivery(
    *,
    database_engine: Any,
    context: TenantContext,
    step_id: UUID,
    revision: int,
    read_result: Any = None,
) -> None:
    """A lost ACK can repeat this transaction without another effect or continuation."""
    with Session(database_engine) as session, session.begin():
        locator = session.exec(
            select(ExecutionStep.unit_id).where(
                ExecutionStep.id == step_id,
                ExecutionStep.tenant_id == context.tenant_id,
            )
        ).one_or_none()
        if locator is None:
            raise DomainError("resource_not_found", "执行步骤不存在")
        session.exec(
            select(SubmissionUnit)
            .where(
                SubmissionUnit.tenant_id == context.tenant_id,
                SubmissionUnit.unit_id == locator,
            )
            .with_for_update()
        ).one()
        step = session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.id == step_id,
                ExecutionStep.tenant_id == context.tenant_id,
            )
            .with_for_update()
        ).one_or_none()
        if step is None:
            raise DomainError("resource_not_found", "执行步骤不存在")
        row = session.get(Submission, step.submission_id)
        if row is None or row.actor_id != context.actor_id:
            raise DomainError("action_forbidden", "提交操作者不匹配")
        if step.dispatch_revision != revision or step.dispatch_id is None:
            return
        if read_result is not None and read_result.state == "STALE":
            return
        # A competing reader owns a lease on the same source. Keep this durable
        # message intact; periodic repair will deliver it after the owner settles.
        if read_result is not None and read_result.state == "BUSY":
            return
        if read_result is not None:
            if read_result.needs_more:
                queue_step(
                    session,
                    step=step,
                    submission=row,
                    reconcile=True,
                    delay=read_result.retry_after_seconds,
                )
            else:
                step.resolved = {**step.resolved, "dispatch_reconciliation_done": True}
                step.dispatch_id = None
                if read_result.state == "WAITING" and step.kind == "READBACK":
                    step.status, step.phase = "PENDING", "IDLE"
                    step.error_code = "dependency_pending"
        elif step.status == "RUNNING":
            return
        else:
            if (
                step.kind == "MATERIAL"
                and step.status == "PENDING"
                and step.distribution_id
            ):
                dist = session.get(MaterialDistribution, step.distribution_id)
                if (
                    dist
                    and dist.tenant_id == step.tenant_id
                    and dist.status == "result_unknown"
                ):
                    step.status, step.phase, step.error_code = (
                        "UNKNOWN",
                        "DONE",
                        "material_result_unknown",
                    )
                    evidence(
                        session, step=step, claim=None, conclusion="MATERIAL_UNKNOWN"
                    )
            if step.status in {"PENDING", "RETRYABLE", "QUEUED"}:
                delay = max(0, ceil((step.due_at - datetime.now(UTC)).total_seconds()))
                queue_step(session, step=step, submission=row, delay=delay)
            elif step.status == "UNKNOWN" and step.kind != "MATERIAL":
                queue_step(session, step=step, submission=row, reconcile=True)
            else:
                step.dispatch_id = None
        step.updated_at = datetime.now(UTC)
        session.add(step)
        submission_id = step.submission_id
        attention = step.status == "UNKNOWN" or step.mismatch
        # Continuation and receipt acknowledgment commit atomically. A crash
        # cannot leave a successful step with no way to wake its dependents.
        wake_unit(session, unit_id=step.unit_id, context=context)
    finalize_submission(
        database_engine=database_engine,
        context=context,
        submission_id=submission_id,
        attention=attention,
    )


def deliver_step(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    payload: dict[str, Any],
    reconcile: bool = False,
) -> None:
    identity, revision = payload_identity(payload, "step_id")
    result = None
    if reconcile:
        result = process_reconciliation(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            step_id=identity,
            revision=revision,
        )
    else:
        process_step(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            step_id=identity,
            revision=revision,
        )
    finish_delivery(
        database_engine=database_engine,
        context=context,
        step_id=identity,
        revision=revision,
        read_result=result,
    )


@celery_app.task(
    name=UNIT_TASK,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)  # type: ignore[untyped-decorator]
def execute_unit_task(
    *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    process_unit(
        database_engine=engine, context=_scope(tenant_id, actor_id), payload=payload
    )


@celery_app.task(
    name=STEP_TASK,
    time_limit=45,
    soft_time_limit=40,
    acks_late=True,
    reject_on_worker_lost=True,
)  # type: ignore[untyped-decorator]
def execute_step_task(
    *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    with Redis.from_url(settings.REDIS_URL) as client:
        deliver_step(
            database_engine=engine,
            redis_client=client,
            context=_scope(tenant_id, actor_id),
            payload=payload,
        )


@celery_app.task(
    name=READ_TASK,
    time_limit=45,
    soft_time_limit=40,
    acks_late=True,
    reject_on_worker_lost=True,
)  # type: ignore[untyped-decorator]
def reconcile_step_task(
    *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    with Redis.from_url(settings.REDIS_URL) as client:
        deliver_step(
            database_engine=engine,
            redis_client=client,
            context=_scope(tenant_id, actor_id),
            payload=payload,
            reconcile=True,
        )


@celery_app.task(name="builds.repair_execution", time_limit=60, soft_time_limit=55)  # type: ignore[untyped-decorator]
def repair_execution_task() -> int:
    return repair_execution(database_engine=engine)
