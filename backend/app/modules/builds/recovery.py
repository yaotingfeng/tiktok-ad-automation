"""Permanent recovery requests; bounded local scheduling, never provider calls."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.models import TenantBC
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_schemas import Recovery
from app.modules.builds.execution_state import evidence
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.recovery_models import (
    RecoveryKind,
    RecoveryReceipt,
    SubmissionRecovery,
    SubmissionRecoveryRequest,
)
from app.modules.tenants.permissions import require_tenant

TASK_NAME = "builds.recover_submission"
register_dispatch_task(TASK_NAME, "control")
PAGE_SIZE = 100
LEASE_SECONDS = 90
REPAIR_SECONDS = 120

# These are repairs of frozen input, not retryable transport failures.
INTENT_ERRORS = "'scene_intent_changed','scene_no_longer_supported','new_preview_required','copy_too_long','blank_copy','invalid_copy'"
COVER_SCOPE = """c.id=s.cover_job_id AND c.tenant_id=s.tenant_id AND c.bc_id=s.bc_id
AND c.material_id=s.material_id AND c.advertiser_id=u.advertiser_id AND c.connection_id=u.connection_id"""
COVER_RETRY = f"""EXISTS (SELECT 1 FROM material_cover_job c WHERE {COVER_SCOPE}
AND c.status='BLOCKED' AND c.request_armed_at IS NULL AND c.known_image_id IS NULL
AND c.dispatch_id IS NULL AND (c.claimed_until IS NULL OR c.claimed_until <= :now)
AND NOT EXISTS (SELECT 1 FROM material_cover_receipt cr WHERE cr.tenant_id=c.tenant_id AND cr.job_id=c.id))"""
BASE = """
FROM execution_step s JOIN build_unit u ON u.tenant_id=s.tenant_id AND u.preview_id=s.preview_id AND u.id=s.unit_id
JOIN submission_unit su ON su.tenant_id=s.tenant_id AND su.submission_id=s.submission_id AND su.unit_id=s.unit_id
WHERE s.tenant_id=:tenant AND s.submission_id=:submission AND su.expanded AND su.disposition='INCLUDED'
AND s.dispatch_id IS NULL AND (s.lease_expires_at IS NULL OR s.lease_expires_at <= :now)
"""
ACCOUNT = """
AND u.connection_id=(SELECT a.connection_id FROM bc_account_access a
 JOIN tiktok_connection c ON c.tenant_id=a.tenant_id AND c.id=a.connection_id
 JOIN tenant_bc b ON b.tenant_id=a.tenant_id AND b.bc_id=a.bc_id
 JOIN advertiser_account aa ON aa.tenant_id=a.tenant_id AND aa.advertiser_id=a.advertiser_id
 WHERE a.tenant_id=s.tenant_id AND a.bc_id=s.bc_id AND a.advertiser_id=u.advertiser_id
 AND a.in_bc AND a.authorized AND a.active AND a.can_build AND a.permission_state='VERIFIED'
 AND c.status='ACTIVE' AND NOT b.ownership_conflict AND NOT aa.ownership_conflict
 AND aa.remote_status IN ('ENABLE','STATUS_ENABLE') AND trim(aa.currency)<>'' AND trim(aa.timezone)<>''
 AND aa.currency=u.currency AND aa.timezone=u.timezone ORDER BY a.connection_id LIMIT 1)
"""
GROUP_READY = """EXISTS (SELECT 1 FROM planned_group g
 WHERE g.tenant_id=s.tenant_id AND g.preview_id=s.preview_id AND g.unit_id=s.unit_id
 AND (s.kind='CAMPAIGN' OR g.id=s.group_id)
 AND EXISTS (SELECT 1 FROM preview_group_material gm WHERE gm.tenant_id=g.tenant_id AND gm.preview_id=g.preview_id AND gm.drama_id=g.drama_id AND gm.group_no=g.group_no)
 AND NOT EXISTS (SELECT 1 FROM preview_group_material gm
 WHERE gm.tenant_id=g.tenant_id AND gm.preview_id=g.preview_id AND gm.drama_id=g.drama_id AND gm.group_no=g.group_no
 AND NOT EXISTS (SELECT 1 FROM execution_step ms WHERE ms.tenant_id=s.tenant_id AND ms.submission_id=s.submission_id AND ms.unit_id=s.unit_id AND ms.kind='MATERIAL' AND ms.material_id=gm.material_id AND ms.status='SUCCEEDED')))
"""
RETRY = f"""
AND s.status IN ('FAILED','RETRYABLE') AND s.request_body IS NULL AND s.remote_id IS NULL
AND s.phase<>'REQUEST_ARMED' AND s.kind<>'READBACK' AND coalesce(s.error_code,'') NOT IN ({INTENT_ERRORS})
AND NOT EXISTS (SELECT 1 FROM step_evidence e WHERE e.tenant_id=s.tenant_id AND e.submission_id=s.submission_id AND e.step_id=s.id
 AND (e.conclusion IN ('REQUEST_ARMED','CREATED','LATE_CREATED') OR e.summary ? 'remote_id'))
AND (s.kind<>'MATERIAL' OR (s.cover_job_id IS NULL AND s.distribution_id IS NULL) OR {COVER_RETRY})
AND (s.parent_step_id IS NULL OR EXISTS (SELECT 1 FROM execution_step p WHERE p.tenant_id=s.tenant_id AND p.submission_id=s.submission_id AND p.id=s.parent_step_id AND p.status='SUCCEEDED' AND p.remote_id IS NOT NULL))
AND (s.kind<>'CAMPAIGN' OR EXISTS (SELECT 1 FROM execution_step cta WHERE cta.tenant_id=s.tenant_id AND cta.submission_id=s.submission_id AND cta.unit_id=s.unit_id AND cta.kind='CTA' AND cta.status='SUCCEEDED' AND cta.remote_id IS NOT NULL))
AND (s.kind NOT IN ('CAMPAIGN','ADGROUP') OR {GROUP_READY})
"""
RECONCILE = f"""
AND (s.status='UNKNOWN' OR s.mismatch OR (s.kind='MATERIAL' AND s.status='FAILED') OR (s.kind='READBACK' AND s.status<>'SUCCEEDED' AND EXISTS
 (SELECT 1 FROM execution_step p WHERE p.tenant_id=s.tenant_id AND p.submission_id=s.submission_id AND p.id=s.parent_step_id AND p.remote_id IS NOT NULL)))
AND (s.kind<>'MATERIAL' OR (s.cover_job_id IS NULL AND EXISTS (SELECT 1 FROM material_distribution d JOIN material_asset_operation o
 ON o.id=d.operation_id AND o.tenant_id=d.tenant_id AND o.bc_id=d.bc_id AND o.material_id=d.material_id AND o.advertiser_id=d.advertiser_id
 WHERE d.id=s.distribution_id AND d.tenant_id=s.tenant_id AND d.bc_id=s.bc_id AND d.material_id=s.material_id AND d.advertiser_id=u.advertiser_id
 AND d.status IN ('result_unknown','verifying','preparing','ready','blocked') AND o.status IN ('sending','result_unknown','verifying','succeeded')))
 OR EXISTS (SELECT 1 FROM material_cover_job c WHERE {COVER_SCOPE}
 AND c.dispatch_id IS NULL AND (c.claimed_until IS NULL OR c.claimed_until <= :now)
 AND (c.request_armed_at IS NOT NULL OR c.known_image_id IS NOT NULL
 OR EXISTS (SELECT 1 FROM material_cover_receipt cr WHERE cr.tenant_id=c.tenant_id AND cr.job_id=c.id))))
"""


def _params(row: Submission) -> dict[str, Any]:
    return {"tenant": row.tenant_id, "submission": row.id, "now": datetime.now(UTC)}


def _query(_row: Submission, kind: str, *, account: bool = True) -> str:
    return (
        BASE + (RETRY if kind == "RETRY" else RECONCILE) + (ACCOUNT if account else "")
    )


def _submission(session: Session, context: TenantContext, identity: UUID) -> Submission:
    row = session.exec(
        select(Submission).where(
            Submission.id == identity, Submission.tenant_id == context.tenant_id
        )
    ).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "提交不存在")
    return row


def _authority(
    session: Session, context: TenantContext, row: Submission
) -> TenantContext:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    original = require_tenant(
        session, actor_id=row.actor_id, tenant_id=row.tenant_id, action="build"
    )
    bc = session.get(TenantBC, (row.tenant_id, row.bc_id), populate_existing=True)
    if bc is None or bc.ownership_conflict:
        raise DomainError("account_ownership_conflict", "提交 BC 当前不可操作")
    return original


def _receipt(
    row: SubmissionRecovery | SubmissionRecoveryRequest, *, original: bool
) -> RecoveryReceipt:
    return RecoveryReceipt.model_validate(
        {
            "recovery_id": row.recovery_id
            if isinstance(row, SubmissionRecoveryRequest)
            else row.id,
            "request_id": row.request_id,
            "submission_id": row.submission_id,
            "kind": row.kind,
            "state": "QUEUED" if original else cast(SubmissionRecovery, row).state,
            "scheduled_count": 0
            if original
            else cast(SubmissionRecovery, row).scheduled_count,
            "reason_code": None
            if original
            else cast(SubmissionRecovery, row).reason_code,
        }
    )


def get_request(
    session: Session, *, context: TenantContext, request_id: UUID
) -> RecoveryReceipt:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    row = session.get(SubmissionRecoveryRequest, (context.tenant_id, request_id))
    if row is None:
        raise DomainError("resource_not_found", "恢复请求不存在")
    return _receipt(row, original=True)


def get_recovery(
    session: Session, *, context: TenantContext, recovery_id: UUID
) -> RecoveryReceipt:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    row = session.exec(
        select(SubmissionRecovery).where(
            SubmissionRecovery.tenant_id == context.tenant_id,
            SubmissionRecovery.id == recovery_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "恢复任务不存在")
    return _receipt(row, original=False)


def queue_recovery(
    session: Session, job: SubmissionRecovery, *, delay: int = 0
) -> None:
    job.due_at = datetime.now(UTC) + timedelta(seconds=delay)
    job.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=job.tenant_id, actor_id=job.request_actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"submission-recovery:{job.id}:{job.dispatch_revision}",
        payload={"recovery_id": str(job.id), "revision": job.dispatch_revision},
    )
    dispatch = session.get(PendingDispatch, job.dispatch_id)
    assert dispatch
    dispatch.available_at = job.due_at
    job.repair_after = job.due_at + timedelta(seconds=REPAIR_SECONDS)
    session.add_all([job, dispatch])


def request_recovery(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    request_id: UUID,
    kind: RecoveryKind,
) -> RecoveryReceipt:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    SASession.execute(
        session,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"build-recovery:{context.tenant_id}:{request_id}"},
    )
    old = session.get(SubmissionRecoveryRequest, (context.tenant_id, request_id))
    if old:
        if old.submission_id != submission_id or old.kind != kind:
            raise DomainError("idempotency_conflict", "恢复请求已绑定其他意图")
        return _receipt(old, original=True)
    row = _submission(session, context, submission_id)
    _authority(session, context, row)
    if not SASession.execute(
        session, text("SELECT EXISTS(SELECT 1 " + _query(row, kind) + ")"), _params(row)
    ).scalar_one():
        raise DomainError("recovery_no_candidates", "当前没有可安全恢复的步骤")
    job = SubmissionRecovery(
        tenant_id=row.tenant_id,
        submission_id=row.id,
        preview_id=row.preview_id,
        bc_id=row.bc_id,
        request_id=request_id,
        request_actor_id=context.actor_id,
        kind=kind,
    )
    session.add(job)
    session.flush()
    ledger = SubmissionRecoveryRequest(
        tenant_id=row.tenant_id,
        request_id=request_id,
        recovery_id=job.id,
        submission_id=row.id,
        bc_id=row.bc_id,
        kind=kind,
    )
    session.add(ledger)
    queue_recovery(session, job)
    session.flush()
    return _receipt(ledger, original=True)


def recovery_summary(
    session: Session, *, context: TenantContext, submission: Submission
) -> Recovery:
    try:
        _authority(session, context, submission)
    except DomainError as error:
        return Recovery(reasons=[error.code])
    params = _params(submission)
    values = (
        SASession.execute(
            session,
            text(
                "SELECT (SELECT count(*) "
                + _query(submission, "RETRY")
                + ") retryable, (SELECT count(*) "
                + _query(submission, "RECONCILE")
                + ") reconcilable"
            ),
            params,
        )
        .mappings()
        .one()
    )
    counts = int(values["retryable"]), int(values["reconcilable"])
    reasons: list[str] = []
    if not any(counts):
        blocked = SASession.execute(
            session,
            text(
                "SELECT EXISTS(SELECT 1 "
                + _query(submission, "RETRY", account=False)
                + ") OR EXISTS(SELECT 1 "
                + _query(submission, "RECONCILE", account=False)
                + ")"
            ),
            params,
        ).scalar_one()
        reasons = ["account_access_denied" if blocked else "recovery_no_candidates"]
    return Recovery(
        retryable_step_count=counts[0],
        reconcilable_step_count=counts[1],
        can_retry=counts[0] > 0,
        can_reconcile=counts[1] > 0,
        reasons=reasons,
    )


def _job(
    session: Session, context: TenantContext, identity: UUID
) -> SubmissionRecovery:
    row = session.exec(
        select(SubmissionRecovery)
        .where(
            SubmissionRecovery.tenant_id == context.tenant_id,
            SubmissionRecovery.id == identity,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "恢复任务不存在")
    if row.request_actor_id != context.actor_id:
        raise DomainError("action_forbidden", "恢复请求操作者不匹配")
    return row


def _owned(job: SubmissionRecovery, token: UUID, revision: int) -> bool:
    return (
        job.state == "RUNNING"
        and job.lease_token == token
        and job.dispatch_revision == revision
        and job.lease_expires_at is not None
        and job.lease_expires_at > datetime.now(UTC)
    )


def _material_reconciliation(
    session: Session, *, step: ExecutionStep, unit: BuildUnit, context: TenantContext
) -> bool:
    if step.cover_job_id:
        from app.modules.builds.cover_execution import cover_matches_step
        from app.modules.materials.cover_models import MaterialCoverJob
        from app.modules.materials.covers import request_cover_reconciliation

        cover = session.get(MaterialCoverJob, step.cover_job_id)
        if cover is None or not cover_matches_step(cover, step, unit):
            return False
        request_cover_reconciliation(session, context=context, job_id=cover.id)
        step.status, step.phase, step.error_code = (
            "UNKNOWN",
            "DONE",
            "cover_result_unknown",
        )
        step.lease_token = step.lease_expires_at = None
        session.add(step)
        evidence(
            session,
            step=step,
            claim=None,
            conclusion="COVER_RECONCILIATION_REQUESTED",
            summary={"cover_job_id": str(cover.id)},
        )
        return True

    from app.modules.materials.distribution import queue_distribution
    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialDistribution,
    )

    dist = session.get(MaterialDistribution, step.distribution_id)
    if dist is None or (
        dist.tenant_id,
        dist.bc_id,
        dist.material_id,
        dist.advertiser_id,
    ) != (step.tenant_id, step.bc_id, step.material_id, unit.advertiser_id):
        return False
    require_tenant(
        session, actor_id=dist.actor_id, tenant_id=step.tenant_id, action="build"
    )
    operation = session.exec(
        select(MaterialAssetOperation)
        .where(
            MaterialAssetOperation.id == dist.operation_id,
            MaterialAssetOperation.tenant_id == step.tenant_id,
        )
        .with_for_update()
    ).one_or_none()
    if operation is None or operation.status not in {
        "sending",
        "result_unknown",
        "verifying",
        "succeeded",
    }:
        return False
    queue_distribution(
        session, dist, operation, kind="verify", observe=True, read_only=True
    )
    step.status, step.phase, step.error_code = (
        "UNKNOWN",
        "DONE",
        "material_result_unknown",
    )
    step.lease_token, step.lease_expires_at = None, None
    session.add(step)
    evidence(
        session,
        step=step,
        claim=None,
        conclusion="MATERIAL_RECONCILIATION_REQUESTED",
        summary={"distribution_id": str(dist.id)},
    )
    return True


def _schedule(
    session: Session,
    *,
    job: SubmissionRecovery,
    row: Submission,
    step_id: UUID,
    original: TenantContext,
) -> bool:
    from app.modules.builds.dispatch import queue_step, wake_unit

    locator = session.exec(
        select(
            ExecutionStep.unit_id, ExecutionStep.parent_step_id, ExecutionStep.kind
        ).where(
            ExecutionStep.tenant_id == row.tenant_id,
            ExecutionStep.submission_id == row.id,
            ExecutionStep.id == step_id,
        )
    ).one_or_none()
    if locator is None:
        return False
    session.exec(
        select(SubmissionUnit)
        .where(
            SubmissionUnit.tenant_id == row.tenant_id,
            SubmissionUnit.submission_id == row.id,
            SubmissionUnit.unit_id == locator[0],
        )
        .with_for_update()
    ).one()
    # Readback reconciliation owns source before child; never reverse that order.
    if locator[2] == "READBACK" and locator[1]:
        session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.tenant_id == row.tenant_id, ExecutionStep.id == locator[1]
            )
            .with_for_update()
        ).one()
    step = session.exec(
        select(ExecutionStep)
        .where(ExecutionStep.tenant_id == row.tenant_id, ExecutionStep.id == step_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    parameters = {**_params(row), "step": step_id}
    if not SASession.execute(
        session,
        text("SELECT EXISTS(SELECT 1 " + _query(row, job.kind) + " AND s.id=:step)"),
        parameters,
    ).scalar_one():
        return False
    unit = session.get(BuildUnit, step.unit_id)
    assert unit
    access = resolve_account_access(
        session,
        context=original,
        bc_id=row.bc_id,
        advertiser_id=unit.advertiser_id,
        action="build",
    )
    if access.connection_id != unit.connection_id or (
        access.currency,
        access.timezone,
    ) != (unit.currency, unit.timezone):
        return False
    if job.kind == "RECONCILE" and step.kind == "MATERIAL":
        return _material_reconciliation(session, step=step, unit=unit, context=original)
    if job.kind == "RETRY":
        if step.kind == "AD":
            from app.modules.builds.cover_execution import retry_ad_covers

            retry_ad_covers(session, context=original, step=step, unit=unit)
        if step.kind == "MATERIAL" and step.cover_job_id:
            from app.modules.materials.covers import request_cover_retry

            request_cover_retry(session, context=original, job_id=step.cover_job_id)
        step.phase, step.lease_token, step.lease_expires_at, step.error_code = (
            "IDLE",
            None,
            None,
            None,
        )
        evidence(
            session,
            step=step,
            claim=None,
            conclusion="RETRY_REQUESTED",
            summary={"recovery_id": str(job.id)},
        )
    else:
        evidence(
            session,
            step=step,
            claim=None,
            conclusion="RECONCILIATION_REQUESTED",
            summary={"recovery_id": str(job.id)},
        )
    queue_step(session, step=step, submission=row, reconcile=job.kind == "RECONCILE")
    wake_unit(session, unit_id=step.unit_id, context=original)
    return True


def process_recovery(
    *, database_engine: Any, context: TenantContext, payload: dict[str, Any]
) -> None:
    from app.modules.builds.dispatch import payload_identity

    identity, revision = payload_identity(payload, "recovery_id")
    token = uuid4()
    with Session(database_engine) as session, session.begin():
        job = _job(session, context, identity)
        if (
            job.state in {"COMPLETED", "FAILED"}
            or job.dispatch_revision != revision
            or job.due_at > datetime.now(UTC)
        ):
            return
        if job.lease_expires_at and job.lease_expires_at > datetime.now(UTC):
            return
        job.state, job.lease_token = "RUNNING", token
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
        job.repair_after = datetime.now(UTC) + timedelta(seconds=REPAIR_SECONDS)
        session.add(job)
    try:
        with Session(database_engine) as session:
            loaded_job = session.get(SubmissionRecovery, identity)
            assert loaded_job
            job = loaded_job
            row = _submission(session, context, job.submission_id)
            _authority(session, context, row)
            parameters = {**_params(row), "cursor": job.cursor_step_id}
            identities = list(
                SASession.execute(
                    session,
                    text(
                        "SELECT s.id "
                        + _query(row, job.kind)
                        + " AND (CAST(:cursor AS uuid) IS NULL OR s.id>CAST(:cursor AS uuid)) ORDER BY s.id LIMIT 100"
                    ),
                    parameters,
                ).scalars()
            )
        for step_id in identities:
            with Session(database_engine) as session, session.begin():
                job = _job(session, context, identity)
                if not _owned(job, token, revision):
                    return
                row = _submission(session, context, job.submission_id)
                original = _authority(session, context, row)
                scheduled = _schedule(
                    session, job=job, row=row, step_id=step_id, original=original
                )
                job.cursor_step_id = step_id
                job.scanned_count += 1
                job.scheduled_count += int(scheduled)
                job.updated_at = datetime.now(UTC)
                session.add(job)
        with Session(database_engine) as session, session.begin():
            job = _job(session, context, identity)
            if not _owned(job, token, revision):
                return
            job.lease_token, job.lease_expires_at = None, None
            job.updated_at = datetime.now(UTC)
            if len(identities) == PAGE_SIZE:
                job.dispatch_revision += 1
                queue_recovery(session, job)
            else:
                job.state, job.finished_at, job.dispatch_id = (
                    "COMPLETED",
                    datetime.now(UTC),
                    None,
                )
                session.add(job)
    except DomainError as error:
        with Session(database_engine) as session, session.begin():
            job = _job(session, context, identity)
            if _owned(job, token, revision):
                job.state, job.reason_code = "FAILED", error.code
                job.finished_at, job.updated_at = datetime.now(UTC), datetime.now(UTC)
                job.lease_token, job.lease_expires_at, job.dispatch_id = (
                    None,
                    None,
                    None,
                )
                session.add(job)


def repair_recoveries(*, database_engine: Any, limit: int = 100) -> int:
    from sqlalchemy import or_
    from sqlmodel import col

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now = datetime.now(UTC)
    with Session(database_engine) as session, session.begin():
        jobs = session.exec(
            select(SubmissionRecovery)
            .where(
                col(SubmissionRecovery.state).in_(["QUEUED", "RUNNING"]),
                SubmissionRecovery.repair_after <= now,
                or_(
                    col(SubmissionRecovery.lease_expires_at).is_(None),
                    col(SubmissionRecovery.lease_expires_at) <= now,
                ),
            )
            .order_by(col(SubmissionRecovery.repair_after), col(SubmissionRecovery.id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            dispatch = session.exec(
                select(PendingDispatch)
                .where(PendingDispatch.id == job.dispatch_id)
                .with_for_update()
            ).one_or_none()
            expected = {"recovery_id": str(job.id), "revision": job.dispatch_revision}
            if dispatch is None:
                queue_recovery(session, job)
            elif (
                dispatch.tenant_id,
                dispatch.actor_id,
                dispatch.task_name,
                dispatch.payload,
            ) != (job.tenant_id, job.request_actor_id, TASK_NAME, expected):
                job.state, job.reason_code = "FAILED", "dispatch_payload_invalid"
                job.lease_token, job.lease_expires_at = None, None
                job.finished_at = now
            elif dispatch.published_at is not None:
                dispatch.published_at, dispatch.available_at = None, now
                session.add(dispatch)
            # Unpublished current generation retains publisher backoff and ID.
            job.repair_after = now + timedelta(seconds=REPAIR_SECONDS)
            session.add(job)
        return len(jobs)
