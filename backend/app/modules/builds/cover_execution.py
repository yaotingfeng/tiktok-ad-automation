"""Publish verified cover results to the original frozen MATERIAL steps."""

from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.accounts.access import resolve_account_access
from app.modules.builds.dispatch import finalize_submission, wake_unit
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_state import evidence
from app.modules.builds.preview_models import BuildUnit
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.tenants.permissions import require_tenant


def cover_matches_step(
    job: MaterialCoverJob, step: ExecutionStep, unit: BuildUnit
) -> bool:
    return (
        job.tenant_id,
        job.bc_id,
        job.material_id,
        job.advertiser_id,
        job.connection_id,
    ) == (
        step.tenant_id,
        step.bc_id,
        step.material_id,
        unit.advertiser_id,
        unit.connection_id,
    )


def recover_cover_results(*, database_engine: Any, limit: int = 100) -> int:
    from app.modules.materials.covers import get_cover_status

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid cover recovery page")
    with Session(database_engine) as session:
        candidates = session.exec(
            select(ExecutionStep.tenant_id, ExecutionStep.unit_id, ExecutionStep.id)
            .join(
                MaterialCoverJob,
                (col(MaterialCoverJob.id) == ExecutionStep.cover_job_id)
                & (col(MaterialCoverJob.tenant_id) == ExecutionStep.tenant_id),
            )
            .where(
                ExecutionStep.kind == "MATERIAL",
                ExecutionStep.status == "UNKNOWN",
                col(MaterialCoverJob.status).in_(["READY", "BLOCKED"]),
            )
            .order_by(col(ExecutionStep.updated_at), col(ExecutionStep.id))
            .limit(limit)
        ).all()
    changed, submissions = 0, {}
    for tenant_id, unit_id, identity in candidates:
        with Session(database_engine) as session, session.begin():
            owner = session.exec(
                select(SubmissionUnit)
                .where(
                    SubmissionUnit.tenant_id == tenant_id,
                    SubmissionUnit.unit_id == unit_id,
                )
                .with_for_update(skip_locked=True)
            ).one_or_none()
            if owner is None:
                continue
            step = session.exec(
                select(ExecutionStep)
                .where(
                    ExecutionStep.tenant_id == tenant_id,
                    ExecutionStep.id == identity,
                )
                .with_for_update()
            ).one()
            if (
                step.status != "UNKNOWN"
                or step.kind != "MATERIAL"
                or step.cover_job_id is None
            ):
                continue
            job, unit = (
                session.get(MaterialCoverJob, step.cover_job_id),
                session.get(BuildUnit, step.unit_id),
            )
            if job is None or unit is None or not cover_matches_step(job, step, unit):
                continue
            row = session.get(Submission, step.submission_id)
            assert row
            context = TenantContext(
                tenant_id=row.tenant_id, actor_id=row.actor_id, role="operator"
            )
            try:
                require_tenant(
                    session,
                    actor_id=context.actor_id,
                    tenant_id=context.tenant_id,
                    action="build",
                )
                access = resolve_account_access(
                    session,
                    context=context,
                    bc_id=step.bc_id,
                    advertiser_id=unit.advertiser_id,
                    action="build",
                )
                if (access.connection_id, access.currency, access.timezone) != (
                    unit.connection_id,
                    unit.currency,
                    unit.timezone,
                ):
                    raise DomainError("new_preview_required", "账户与冻结预览不一致")
                result = get_cover_status(session, context=context, job_id=job.id)
            except DomainError as error:
                step.error_code, step.updated_at = error.code, datetime.now(UTC)
                session.add(step)
                continue
            if (
                result.state == "ready"
                and result.mapping
                and result.mapping.image_id
                and result.mapping.video_id == job.video_id
                and result.mapping.connection_id == unit.connection_id
            ):
                step.status, step.phase, step.error_code = "SUCCEEDED", "DONE", None
                step.resolved = {
                    **step.resolved,
                    "mapping": result.mapping.model_dump(mode="json"),
                }
                conclusion = "MATERIAL_COVER_VERIFIED"
            elif (
                job.status == "BLOCKED"
                and job.request_armed_at is None
                and job.known_image_id is None
            ):
                step.status, step.phase, step.error_code = (
                    "FAILED",
                    "DONE",
                    result.reason_code or "cover_blocked",
                )
                conclusion = "MATERIAL_COVER_BLOCKED"
            else:
                step.error_code, step.updated_at = (
                    result.reason_code or "cover_result_unknown",
                    datetime.now(UTC),
                )
                session.add(step)
                continue
            step.dispatch_id = step.lease_token = step.lease_expires_at = None
            step.updated_at = datetime.now(UTC)
            session.add(step)
            evidence(
                session,
                step=step,
                claim=None,
                conclusion=conclusion,
                summary={"cover_job_id": str(job.id)},
            )
            wake_unit(session, unit_id=unit_id, context=context)
            submissions[row.id] = context
            changed += 1
    for submission_id, context in submissions.items():
        finalize_submission(
            database_engine=database_engine,
            context=context,
            submission_id=submission_id,
        )
    return changed
