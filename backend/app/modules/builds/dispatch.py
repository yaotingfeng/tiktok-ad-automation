"""Bounded unit scheduling and durable step delivery, separate from SDK calls."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_state import evidence, expire_attempt
from app.modules.builds.submission_tasks import queue_execution_unit
from app.modules.builds.submissions import (
    aggregate_status,
    get_submission,
    group_material_state,
)
from app.modules.tenants.permissions import require_tenant

STEP_TASK = "builds.execute_step"
READ_TASK = "builds.reconcile_step"
UNIT_TASK = "builds.execute_unit"
for name in (STEP_TASK, READ_TASK, UNIT_TASK):
    register_dispatch_task(name, "builds")
REPAIR_SECONDS = 120


def payload_identity(payload: dict[str, Any], key: str) -> tuple[UUID, int]:
    if (
        set(payload) != {key, "revision"}
        or type(payload["revision"]) is not int
        or payload["revision"] < 0
    ):
        raise DomainError("dispatch_payload_invalid", "任务参数无效")
    try:
        identity = UUID(payload[key])
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "任务标识无效") from None
    return identity, payload["revision"]


def _context(submission: Submission) -> TenantContext:
    return TenantContext(
        tenant_id=submission.tenant_id, actor_id=submission.actor_id, role="operator"
    )


def queue_step(
    session: Session,
    *,
    step: ExecutionStep,
    submission: Submission,
    reconcile: bool = False,
    delay: int = 0,
) -> None:
    if (step.tenant_id, step.submission_id) != (submission.tenant_id, submission.id):
        raise DomainError("resource_not_found", "执行步骤范围无效")
    if not reconcile and (
        step.remote_id
        or step.request_body is not None
        or step.status in {"UNKNOWN", "SUCCEEDED"}
    ):
        raise DomainError(
            "execution_requires_reconciliation", "该步骤必须先核实远端结果"
        )
    name = READ_TASK if reconcile else STEP_TASK
    step.dispatch_revision += 1
    step.due_at = datetime.now(UTC) + timedelta(seconds=max(0, delay))
    step.updated_at = datetime.now(UTC)
    if reconcile:
        step.resolved = {**step.resolved, "dispatch_reconciliation_done": False}
        if step.kind == "READBACK":
            step.status = "QUEUED"
    else:
        step.status = "QUEUED"
    step.dispatch_id = enqueue_after_commit(
        session,
        context=_context(submission),
        task_name=name,
        task_key=f"build-step:{step.id}:{step.dispatch_revision}",
        payload={"step_id": str(step.id), "revision": step.dispatch_revision},
    )
    dispatch = session.get(PendingDispatch, step.dispatch_id)
    assert dispatch
    dispatch.available_at = step.due_at
    session.add_all([step, dispatch])


def wake_unit(session: Session, *, unit_id: UUID, context: TenantContext) -> None:
    unit = session.exec(
        select(SubmissionUnit)
        .where(
            SubmissionUnit.tenant_id == context.tenant_id,
            SubmissionUnit.unit_id == unit_id,
        )
        .with_for_update()
    ).one_or_none()
    if unit is None or not unit.expanded or unit.disposition != "INCLUDED":
        return
    submission = session.get(Submission, unit.submission_id)
    if submission is None or submission.actor_id != context.actor_id:
        raise DomainError("action_forbidden", "提交操作者不匹配")
    # A predecessor just changed. Make dependent local checks eligible again,
    # leaving transport backoff and in-flight steps untouched.
    SASession.execute(
        session,
        update(ExecutionStep)
        .where(
            col(ExecutionStep.tenant_id) == context.tenant_id,
            col(ExecutionStep.unit_id) == unit_id,
            col(ExecutionStep.status) == "PENDING",
            col(ExecutionStep.error_code).in_(
                ["dependency_pending", "dependency_unknown"]
            ),
        )
        .values(due_at=datetime.now(UTC)),
    )
    current = (
        session.get(PendingDispatch, unit.dispatch_id) if unit.dispatch_id else None
    )
    if (
        current
        and current.published_at is None
        and current.available_at <= datetime.now(UTC)
    ):
        return
    unit.dispatch_revision += 1
    queue_execution_unit(session, submission=submission, unit=unit)


def _dependency(session: Session, context: TenantContext, step: ExecutionStep) -> str:
    if step.kind in {"MATERIAL", "CTA"}:
        return "READY"
    if step.parent_step_id:
        parent = session.get(ExecutionStep, step.parent_step_id)
        if parent is None or parent.status == "FAILED":
            return "FAILED"
        if parent.status == "UNKNOWN":
            return "UNKNOWN"
        if parent.status != "SUCCEEDED" or not parent.remote_id:
            return "PENDING"
    if step.kind == "CAMPAIGN":
        cta = session.exec(
            select(ExecutionStep).where(
                col(ExecutionStep.tenant_id) == step.tenant_id,
                col(ExecutionStep.unit_id) == step.unit_id,
                ExecutionStep.kind == "CTA",
            )
        ).one()
        if cta.status in {"FAILED", "UNKNOWN"}:
            return cta.status
        if cta.status != "SUCCEEDED" or not cta.remote_id:
            return "PENDING"
    if step.kind in {"CAMPAIGN", "ADGROUP"}:
        return group_material_state(
            session,
            context=context,
            submission_id=step.submission_id,
            unit_id=step.unit_id,
            group_id=step.group_id,
        )
    return "READY"


def finalize_submission(
    *,
    database_engine: Any,
    context: TenantContext,
    submission_id: UUID,
    attention: bool = False,
) -> None:
    """Aggregate only after all unit schedulers and active steps have settled."""
    with Session(database_engine) as session, session.begin():
        row = session.exec(
            select(Submission)
            .where(
                Submission.tenant_id == context.tenant_id,
                Submission.id == submission_id,
            )
            .with_for_update()
        ).one_or_none()
        if row is None or row.actor_id != context.actor_id:
            return
        active_unit = session.exec(
            select(SubmissionUnit.unit_id)
            .where(
                SubmissionUnit.tenant_id == row.tenant_id,
                SubmissionUnit.submission_id == row.id,
                SubmissionUnit.disposition == "INCLUDED",
                or_(
                    col(SubmissionUnit.expanded).is_(False),
                    col(SubmissionUnit.dispatch_id).is_not(None),
                ),
            )
            .limit(1)
        ).first()
        active_step = session.exec(
            select(ExecutionStep.id)
            .where(
                col(ExecutionStep.tenant_id) == row.tenant_id,
                ExecutionStep.submission_id == row.id,
                col(ExecutionStep.status).in_(["QUEUED", "RUNNING"]),
            )
            .limit(1)
        ).first()
        if row.expanded and active_unit is None and active_step is None:
            try:
                row.status = get_submission(
                    session, context=context, submission_id=row.id
                ).status
            except DomainError:
                # Internal termination retains existing results even when the
                # original actor can no longer read this tenant's public API.
                facts = session.exec(
                    select(ExecutionStep.status, ExecutionStep.mismatch)
                    .where(
                        col(ExecutionStep.tenant_id) == row.tenant_id,
                        ExecutionStep.submission_id == row.id,
                    )
                    .group_by(col(ExecutionStep.status), col(ExecutionStep.mismatch))
                ).all()
                row.status = aggregate_status(
                    {status for status, _ in facts}
                    | ({"MISMATCH"} if any(m for _, m in facts) else set())
                )
        elif attention:
            row.status = "NEEDS_REVIEW"
        elif row.status in {"QUEUED", "COMPLETED", "PARTIAL", "FAILED"}:
            row.status = "RUNNING"
        row.updated_at = datetime.now(UTC)
        session.add(row)


def process_unit(
    *,
    database_engine: Any,
    context: TenantContext,
    payload: dict[str, Any],
    limit: int = 100,
) -> int:
    identity, revision = payload_identity(payload, "unit_id")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid scheduling batch")
    count = 0
    attention = False
    with Session(database_engine) as session, session.begin():
        unit = session.exec(
            select(SubmissionUnit)
            .where(
                SubmissionUnit.tenant_id == context.tenant_id,
                SubmissionUnit.unit_id == identity,
            )
            .with_for_update()
        ).one_or_none()
        if unit is None:
            raise DomainError("resource_not_found", "执行组合不存在")
        row = session.get(Submission, unit.submission_id)
        if row is None or row.actor_id != context.actor_id:
            raise DomainError("action_forbidden", "提交操作者不匹配")
        if (
            unit.dispatch_revision != revision
            or unit.dispatch_id is None
            or not unit.expanded
        ):
            return 0
        now = datetime.now(UTC)
        parent = aliased(ExecutionStep)
        parent_settled = (
            select(parent.id)
            .where(
                parent.tenant_id == context.tenant_id,
                parent.id == ExecutionStep.parent_step_id,
                col(parent.status).in_(["SUCCEEDED", "FAILED"]),
            )
            .exists()
        )
        current_read = (
            select(PendingDispatch.id)
            .where(
                PendingDispatch.id == ExecutionStep.dispatch_id,
                PendingDispatch.task_name == READ_TASK,
            )
            .exists()
        )
        stmt = (
            select(ExecutionStep)
            .where(
                col(ExecutionStep.tenant_id) == context.tenant_id,
                col(ExecutionStep.unit_id) == identity,
                col(ExecutionStep.due_at) <= now,
                or_(
                    and_(
                        col(ExecutionStep.status).in_(["PENDING", "RETRYABLE"]),
                        or_(
                            col(ExecutionStep.parent_step_id).is_(None), parent_settled
                        ),
                    ),
                    and_(
                        col(ExecutionStep.status) == "RUNNING",
                        col(ExecutionStep.lease_expires_at) <= now,
                    ),
                    and_(col(ExecutionStep.status) == "UNKNOWN", ~current_read),
                ),
            )
            .order_by(col(ExecutionStep.due_at), col(ExecutionStep.id))
            .limit(limit + 1)
        )
        permission_error = None
        try:
            require_tenant(
                session,
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                action="build",
            )
        except DomainError as error:
            permission_error = error.code
            stmt = (
                select(ExecutionStep)
                .where(
                    col(ExecutionStep.tenant_id) == context.tenant_id,
                    col(ExecutionStep.unit_id) == identity,
                    col(ExecutionStep.status).in_(
                        ["PENDING", "RETRYABLE", "QUEUED", "RUNNING"]
                    ),
                    col(ExecutionStep.request_body).is_(None),
                    col(ExecutionStep.remote_id).is_(None),
                )
                .order_by(col(ExecutionStep.id))
                .limit(limit + 1)
            )
        candidates = session.exec(stmt.with_for_update(skip_locked=True)).all()
        dependency_failed = False
        for step in candidates[:limit]:
            if permission_error:
                step.status, step.phase, step.error_code = (
                    "FAILED",
                    "DONE",
                    permission_error,
                )
                step.lease_token = step.lease_expires_at = None
                evidence(
                    session,
                    step=step,
                    claim=None,
                    conclusion="LOCAL_FAILED",
                    summary={"reason_code": permission_error},
                )
                session.add(step)
                continue
            if step.status == "RUNNING":
                expire_attempt(session, step=step)
            if step.status == "UNKNOWN":
                attention = True
                queue_step(session, step=step, submission=row, reconcile=True)
                count += 1
                continue
            dependency = _dependency(session, context, step)
            if dependency == "FAILED":
                dependency_failed = True
                step.status, step.phase, step.error_code = (
                    "FAILED",
                    "DONE",
                    "dependency_failed",
                )
                evidence(session, step=step, claim=None, conclusion="DEPENDENCY_FAILED")
                session.add(step)
            elif dependency in {"PENDING", "UNKNOWN"}:
                step.error_code = (
                    "dependency_unknown"
                    if dependency == "UNKNOWN"
                    else "dependency_pending"
                )
                step.due_at = now + timedelta(seconds=15)
                session.add(step)
            else:
                queue_step(
                    session,
                    step=step,
                    submission=row,
                    reconcile=step.kind == "READBACK",
                )
                count += 1
        unit.dispatch_id = None
        session.add(unit)
        if len(candidates) > limit or dependency_failed:
            unit.dispatch_revision += 1
            queue_execution_unit(session, submission=row, unit=unit)
        submission_id = row.id
    finalize_submission(
        database_engine=database_engine,
        context=context,
        submission_id=submission_id,
        attention=attention,
    )
    return count


def _repair_message(
    session: Session, *, step: ExecutionStep, row: Submission, now: datetime
) -> None:
    dispatch = (
        session.get(PendingDispatch, step.dispatch_id) if step.dispatch_id else None
    )
    mode = step.kind == "READBACK" or step.status == "UNKNOWN"
    expected = READ_TASK if mode else STEP_TASK
    if dispatch is None:
        queue_step(session, step=step, submission=row, reconcile=mode)
        return
    if (
        dispatch.tenant_id,
        dispatch.actor_id,
        dispatch.task_name,
        dispatch.payload,
    ) != (
        step.tenant_id,
        row.actor_id,
        expected,
        {"step_id": str(step.id), "revision": step.dispatch_revision},
    ):
        step.status, step.error_code = (
            "UNKNOWN" if step.request_body is not None else "FAILED",
            "dispatch_payload_invalid",
        )
        step.resolved = {**step.resolved, "dispatch_reconciliation_done": True}
        session.add(step)
        return
    if dispatch.published_at is not None:
        dispatch.published_at = None
        dispatch.available_at = now
        session.add(dispatch)
    # Unpublished broker backoff belongs to the outbox and remains unchanged.
    step.updated_at = now
    session.add(step)


def repair_execution(*, database_engine: Any, limit: int = 100) -> int:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid repair batch")
    now = datetime.now(UTC)
    count = 0
    # Step repair never takes unit locks; completion wakes units in a later tx.
    with Session(database_engine) as session, session.begin():
        rows = session.exec(
            select(ExecutionStep)
            .where(
                or_(
                    and_(
                        col(ExecutionStep.status) == "RUNNING",
                        col(ExecutionStep.lease_expires_at) <= now,
                    ),
                    and_(
                        col(ExecutionStep.status).in_(["QUEUED", "PENDING"]),
                        col(ExecutionStep.due_at) <= now,
                        col(ExecutionStep.updated_at)
                        <= now - timedelta(seconds=REPAIR_SECONDS),
                        col(ExecutionStep.dispatch_id).is_not(None),
                    ),
                    and_(
                        col(ExecutionStep.status) == "UNKNOWN",
                        col(ExecutionStep.due_at) <= now,
                        col(ExecutionStep.updated_at)
                        <= now - timedelta(seconds=REPAIR_SECONDS),
                        col(ExecutionStep.resolved)[
                            "dispatch_reconciliation_done"
                        ].astext.is_distinct_from("true"),
                    ),
                )
            )
            .order_by(col(ExecutionStep.due_at), col(ExecutionStep.id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for step in rows:
            row = session.get(Submission, step.submission_id)
            assert row
            if step.status == "RUNNING":
                action = expire_attempt(session, step=step)
                if action == "RECONCILE":
                    queue_step(session, step=step, submission=row, reconcile=True)
                    count += 1
                    continue
            _repair_message(session, step=step, row=row, now=now)
            count += 1
    with Session(database_engine) as session, session.begin():
        units = session.exec(
            select(SubmissionUnit)
            .where(
                col(SubmissionUnit.dispatch_id).is_not(None),
                SubmissionUnit.repair_after <= now,
            )
            .order_by(col(SubmissionUnit.repair_after), col(SubmissionUnit.unit_id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for unit in units:
            row = session.get(Submission, unit.submission_id)
            assert row
            dispatch = session.get(PendingDispatch, unit.dispatch_id)
            if (
                dispatch
                and dispatch.task_name == UNIT_TASK
                and dispatch.payload
                == {"unit_id": str(unit.unit_id), "revision": unit.dispatch_revision}
                and dispatch.tenant_id == unit.tenant_id
                and dispatch.actor_id == row.actor_id
            ):
                if dispatch.published_at is not None:
                    dispatch.published_at = None
                    dispatch.available_at = now
                    session.add(dispatch)
            elif dispatch is None:
                queue_execution_unit(session, submission=row, unit=unit)
            else:
                unit.reason_code = "dispatch_payload_invalid"
                unit.dispatch_id = None
            unit.repair_after = now + timedelta(seconds=REPAIR_SECONDS)
            session.add(unit)
            count += 1
    return count
