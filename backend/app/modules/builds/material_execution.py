"""Reflect verified material outcomes without blocking unrelated recovery work."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.builds.dispatch import finalize_submission, wake_unit
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_state import evidence
from app.modules.builds.preview_models import BuildUnit
from app.modules.materials.models import MaterialAssetOperation, MaterialDistribution
from app.modules.materials.readiness import get_material_readiness
from app.modules.tenants.permissions import require_tenant


def recover_material_results(*, database_engine: Any, limit: int = 100) -> int:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid material recovery page")
    with Session(database_engine) as session:
        candidates = session.exec(
            select(ExecutionStep.tenant_id, ExecutionStep.unit_id, ExecutionStep.id)
            .join(
                MaterialDistribution,
                (col(MaterialDistribution.id) == ExecutionStep.distribution_id)
                & (col(MaterialDistribution.tenant_id) == ExecutionStep.tenant_id),
            )
            .where(
                ExecutionStep.kind == "MATERIAL",
                ExecutionStep.status == "UNKNOWN",
                col(ExecutionStep.cover_job_id).is_(None),
                col(MaterialDistribution.status).in_(["ready", "blocked"]),
            )
            .order_by(col(ExecutionStep.updated_at), col(ExecutionStep.id))
            .limit(limit)
        ).all()
    changed = 0
    submissions: dict[UUID, TenantContext] = {}
    for tenant_id, unit_id, identity in candidates:
        with Session(database_engine) as session, session.begin():
            unit = session.exec(
                select(SubmissionUnit)
                .where(
                    SubmissionUnit.tenant_id == tenant_id,
                    SubmissionUnit.unit_id == unit_id,
                )
                .with_for_update(skip_locked=True)
            ).one_or_none()
            if unit is None:
                continue
            step = session.exec(
                select(ExecutionStep)
                .where(
                    ExecutionStep.tenant_id == tenant_id, ExecutionStep.id == identity
                )
                .with_for_update()
            ).one()
            if (
                step.kind != "MATERIAL"
                or step.status != "UNKNOWN"
                or step.distribution_id is None
                or step.cover_job_id is not None
            ):
                continue
            dist = session.get(MaterialDistribution, step.distribution_id)
            frozen = session.get(BuildUnit, step.unit_id)
            if (
                dist is None
                or (dist.tenant_id, dist.bc_id, dist.material_id, dist.advertiser_id)
                != (
                    step.tenant_id,
                    step.bc_id,
                    step.material_id,
                    frozen.advertiser_id if frozen else None,
                )
                or dist.status not in {"ready", "blocked"}
            ):
                continue
            row = session.get(Submission, step.submission_id)
            assert row
            context = TenantContext(
                tenant_id=row.tenant_id, actor_id=row.actor_id, role="operator"
            )
            denied = None
            try:
                require_tenant(
                    session,
                    actor_id=context.actor_id,
                    tenant_id=context.tenant_id,
                    action="build",
                )
            except DomainError as error:
                denied = error.code
            operation = (
                session.get(MaterialAssetOperation, dist.operation_id)
                if dist.operation_id
                else None
            )
            if denied or (
                dist.status == "blocked"
                and operation
                and operation.status in {"sending", "result_unknown", "verifying"}
            ):
                step.error_code = (
                    denied or dist.reason_code or "material_result_unknown"
                )
                step.updated_at = datetime.now(UTC)
                session.add(step)
                continue
            if dist.status == "ready" and denied is None:
                assert step.material_id and frozen
                readiness = get_material_readiness(
                    session,
                    context=context,
                    bc_id=step.bc_id,
                    material_id=step.material_id,
                    advertiser_id=frozen.advertiser_id,
                )
                if (
                    readiness.state != "ready"
                    or not readiness.mapping
                    or readiness.mapping.connection_id != frozen.connection_id
                ):
                    # A historic distribution receipt is not proof of a current
                    # target mapping. Keep uncertainty; never call an upload-capable
                    # ensure helper to recover an ambiguous upload.
                    step.error_code = "target_asset_requires_reconciliation"
                    step.updated_at = datetime.now(UTC)
                    session.add(step)
                    continue
                if not readiness.mapping.image_id:
                    # The video is now positively verified. Prepare its separate,
                    # durable image dependency; never re-enter video upload recovery.
                    from app.modules.materials.covers import ensure_cover

                    try:
                        cover = ensure_cover(
                            session,
                            context=context,
                            bc_id=step.bc_id,
                            material_id=step.material_id,
                            advertiser_id=frozen.advertiser_id,
                            task_key=f"build-cover:{step.id}",
                        )
                    except DomainError as error:
                        step.error_code, step.updated_at = error.code, datetime.now(UTC)
                        session.add(step)
                        continue
                    if cover.task_id is None:
                        step.error_code = cover.reason_code or "cover_video_not_ready"
                        step.updated_at = datetime.now(UTC)
                        session.add(step)
                        continue
                    step.cover_job_id = cover.task_id
                    step.status, step.phase = "UNKNOWN", "DONE"
                    step.error_code = cover.reason_code or "cover_pending"
                    if cover.state == "queued":
                        step.status, step.phase = "PENDING", "IDLE"
                    step.dispatch_id = step.lease_token = step.lease_expires_at = None
                    step.updated_at = datetime.now(UTC)
                    session.add(step)
                    evidence(
                        session,
                        step=step,
                        claim=None,
                        conclusion="VIDEO_VERIFIED_COVER_PENDING",
                    )
                    wake_unit(session, unit_id=unit_id, context=context)
                    submissions[row.id] = context
                    changed += 1
                    continue
                step.status, step.phase, step.error_code = "SUCCEEDED", "DONE", None
                step.resolved = {
                    **step.resolved,
                    "mapping": readiness.mapping.model_dump(mode="json"),
                }
                step.dispatch_id = None
                conclusion = "MATERIAL_VERIFIED"
            else:
                step.status, step.phase = "FAILED", "DONE"
                step.error_code = denied or dist.reason_code or "material_blocked"
                step.dispatch_id = None
                conclusion = "MATERIAL_BLOCKED"
            step.lease_token = step.lease_expires_at = None
            step.updated_at = datetime.now(UTC)
            session.add(step)
            evidence(session, step=step, claim=None, conclusion=conclusion)
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
