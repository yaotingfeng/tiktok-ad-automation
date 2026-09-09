"""Publish verified cover results to the original frozen MATERIAL steps."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, col, select

from app.core.config import settings
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
from app.modules.builds.preview_models import (
    BuildUnit,
    PlannedGroup,
    PreviewGroupMaterial,
)
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial
from app.modules.materials.readiness import mapping_fresh
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


def retry_ad_covers(
    session: Session, *, context: TenantContext, step: ExecutionStep, unit: BuildUnit
) -> None:
    """An explicit retry of an unsent AD resumes its bounded cover dependencies.

    Completed MATERIAL steps remain historical facts. An uncertain image upload
    always keeps its original identity and receives read-only reconciliation.
    """
    from app.modules.materials.covers import (
        request_cover_reconciliation,
        request_cover_retry,
    )

    if step.kind != "AD" or step.request_body is not None or step.remote_id:
        raise DomainError("execution_requires_reconciliation", "广告结果需要先核实")
    jobs = session.exec(
        select(MaterialCoverJob)
        .join(
            AccountMaterial,
            (col(AccountMaterial.id) == MaterialCoverJob.asset_id)
            & (col(AccountMaterial.tenant_id) == MaterialCoverJob.tenant_id)
            & (col(AccountMaterial.video_id) == MaterialCoverJob.video_id)
            & (col(AccountMaterial.connection_id) == MaterialCoverJob.connection_id),
        )
        .join(
            PreviewGroupMaterial,
            (col(PreviewGroupMaterial.tenant_id) == AccountMaterial.tenant_id)
            & (col(PreviewGroupMaterial.material_id) == AccountMaterial.material_id),
        )
        .join(
            PlannedGroup,
            (col(PlannedGroup.tenant_id) == PreviewGroupMaterial.tenant_id)
            & (col(PlannedGroup.preview_id) == PreviewGroupMaterial.preview_id)
            & (col(PlannedGroup.drama_id) == PreviewGroupMaterial.drama_id)
            & (col(PlannedGroup.group_no) == PreviewGroupMaterial.group_no),
        )
        .where(
            MaterialCoverJob.tenant_id == context.tenant_id,
            MaterialCoverJob.bc_id == step.bc_id,
            MaterialCoverJob.advertiser_id == unit.advertiser_id,
            MaterialCoverJob.connection_id == unit.connection_id,
            PlannedGroup.preview_id == step.preview_id,
            PlannedGroup.unit_id == step.unit_id,
            PlannedGroup.id == step.group_id,
        )
        .order_by(col(MaterialCoverJob.id))
        .limit(50)
    ).all()
    for job in jobs:
        if job.status == "BLOCKED" and job.request_armed_at is None:
            request_cover_retry(session, context=context, job_id=job.id)
        else:
            request_cover_reconciliation(session, context=context, job_id=job.id)


def validate_ad_assets(
    session: Session, *, step: ExecutionStep, unit: BuildUnit, body: dict[str, Any]
) -> None:
    """Pure local final fence, after admission and immediately before AD arming."""
    rows = session.exec(
        select(PreviewGroupMaterial.material_id, AccountMaterial, MaterialCoverJob)
        .select_from(PreviewGroupMaterial)
        .join(
            PlannedGroup,
            (col(PlannedGroup.tenant_id) == PreviewGroupMaterial.tenant_id)
            & (col(PlannedGroup.preview_id) == PreviewGroupMaterial.preview_id)
            & (col(PlannedGroup.drama_id) == PreviewGroupMaterial.drama_id)
            & (col(PlannedGroup.group_no) == PreviewGroupMaterial.group_no),
        )
        .outerjoin(
            AccountMaterial,
            (col(AccountMaterial.tenant_id) == PreviewGroupMaterial.tenant_id)
            & (col(AccountMaterial.material_id) == PreviewGroupMaterial.material_id)
            & (col(AccountMaterial.bc_id) == step.bc_id)
            & (col(AccountMaterial.advertiser_id) == unit.advertiser_id)
            & (col(AccountMaterial.connection_id) == unit.connection_id),
        )
        .outerjoin(
            MaterialCoverJob,
            (col(MaterialCoverJob.tenant_id) == AccountMaterial.tenant_id)
            & (col(MaterialCoverJob.asset_id) == AccountMaterial.id)
            & (col(MaterialCoverJob.connection_id) == AccountMaterial.connection_id)
            & (col(MaterialCoverJob.video_id) == AccountMaterial.video_id),
        )
        .where(
            PlannedGroup.tenant_id == step.tenant_id,
            PlannedGroup.preview_id == step.preview_id,
            PlannedGroup.unit_id == step.unit_id,
            PlannedGroup.id == step.group_id,
        )
        .order_by(col(PreviewGroupMaterial.position))
        .limit(51)
        .execution_options(populate_existing=True)
    ).all()
    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.MATERIAL_ASSET_MAX_AGE_SECONDS
    )
    expected = []
    valid = 1 <= len(rows) <= 50
    for _, mapping, job in rows:
        if not mapping_fresh(mapping) or not mapping or not mapping.image_id:
            valid = False
            continue
        if job and (
            job.status != "READY"
            or job.updated_at < cutoff
            or job.known_image_id != mapping.image_id
        ):
            valid = False
        expected.append((mapping.video_id, mapping.image_id))
    try:
        actual = [
            (
                entry["creative_info"]["video_info"]["video_id"],
                entry["creative_info"]["image_info"][0]["web_uri"],
            )
            for entry in body["creative_list"]
        ]
    except KeyError, TypeError, IndexError:
        raise DomainError("invalid_build_request", "创意素材结构无效") from None
    if not valid or actual != expected:
        raise DomainError(
            "material_refresh_required", "目标素材已变化，需要重新核实", retryable=True
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
