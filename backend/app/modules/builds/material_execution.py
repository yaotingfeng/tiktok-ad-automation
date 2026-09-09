"""Reflect verified material outcomes without blocking unrelated recovery work."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.builds.dispatch import finalize_submission, queue_step, wake_unit
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_state import evidence
from app.modules.builds.preview_models import BuildUnit
from app.modules.materials.models import MaterialDistribution
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
            if dist.status == "ready" and denied is None:
                # The next MATERIAL step uses ensure_target_asset, which must
                # recheck current target mapping/cover/authority before success.
                step.status, step.phase, step.error_code = "PENDING", "IDLE", None
                queue_step(session, step=step, submission=row)
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
