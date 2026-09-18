"""Reflect verified material outcomes without blocking unrelated recovery work."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import tuple_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.local_read_batch import local_read_batch
from app.modules.accounts.routing import verify_route
from app.modules.builds.dispatch import finalize_submission, wake_unit
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_state import evidence
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.routes import load_preview_route
from app.modules.materials.models import MaterialAssetOperation, MaterialDistribution
from app.modules.materials.readiness import get_material_readiness
from app.modules.materials.routes import load_material_route, require_same_route
from app.modules.tenants.permissions import require_tenant


def material_needs_planning(
    session: Session, context: TenantContext, step: ExecutionStep
) -> bool:
    """旧单条消息也不能越过切片规划；已有VID的本地复用/异常核查不受影响。"""
    if step.kind != "MATERIAL" or step.distribution_id is not None:
        return False
    unit = session.get(BuildUnit, step.unit_id)
    if unit is None or unit.tenant_id != context.tenant_id or step.material_id is None:
        return False
    try:
        route = load_preview_route(session, context=context, preview_id=step.preview_id)
        readiness = get_material_readiness(
            session,
            context=context,
            bc_id=step.bc_id,
            material_id=step.material_id,
            advertiser_id=unit.advertiser_id,
            route=route,
        )
        return readiness.state == "preparable" and readiness.mapping is None
    except DomainError:
        # 权限/路由错误交由原执行器记录正式失败，不把失败伪装成规划等待。
        return False


def plan_material_slice(
    *,
    database_engine: Any,
    context: TenantContext,
    unit_id: UUID,
    unit_revision: int | None = None,
) -> int:
    """先原子登记20素材×当前账户窗口，再允许原共享任务发送。

    此入口位于单元调度锁之前，统一按单元、步骤、素材顺序领取；只安排
    已冻结且未发送的依赖，不扩大预览中跳过素材或未来窗口的授权集合。
    """
    from app.modules.builds.execution_window import window_units
    from app.modules.materials.distribution import ensure_target_assets
    from app.modules.materials.models import AccountMaterial, MaterialFile

    with Session(database_engine) as db, db.begin():
        anchor = db.exec(
            select(SubmissionUnit).where(
                SubmissionUnit.tenant_id == context.tenant_id,
                SubmissionUnit.unit_id == unit_id,
            )
        ).one_or_none()
        if anchor is None:
            return 0
        submission = db.get(Submission, anchor.submission_id)
        if submission is None or submission.actor_id != context.actor_id:
            raise DomainError("action_forbidden", "提交操作者不匹配")
        if not submission.expanded:
            return 0
        require_tenant(
            db, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
        )
        scope = window_units().where(
            SubmissionUnit.tenant_id == context.tenant_id,
            SubmissionUnit.submission_id == submission.id,
        )
        selected_ids = [row.unit_id for row in db.exec(scope).all()]
        if unit_id not in selected_ids:
            return 0
        units = db.exec(
            select(SubmissionUnit)
            .where(
                SubmissionUnit.tenant_id == context.tenant_id,
                SubmissionUnit.submission_id == submission.id,
                col(SubmissionUnit.unit_id).in_(selected_ids),
            )
            .order_by(col(SubmissionUnit.unit_id))
            .with_for_update(skip_locked=True)
        ).all()
        if len(units) != len(selected_ids):
            return 0
        # UNIT 消息的代际必须在锁内、任何素材登记之前校验；旧消息只退出。
        db.refresh(anchor)
        if unit_revision is not None and (
            anchor.dispatch_revision != unit_revision
            or anchor.dispatch_id is None
            or not anchor.expanded
        ):
            return 0
        active = db.exec(
            select(ExecutionStep.material_id)
            .join(
                MaterialDistribution,
                col(MaterialDistribution.id) == ExecutionStep.distribution_id,
            )
            .where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.submission_id == submission.id,
                col(ExecutionStep.unit_id).in_(selected_ids),
                col(MaterialDistribution.status).in_(
                    ["queued", "preparing", "verifying"]
                ),
            )
            .distinct()
            .limit(20)
        ).all()
        # 滚动补货而非整片屏障：一个慢上传不能阻塞所有后续素材。
        capacity = 20 - len(active)
        if capacity <= 0:
            return 0
        frozen = {
            row.id: row
            for row in db.exec(
                select(BuildUnit).where(
                    BuildUnit.tenant_id == context.tenant_id,
                    col(BuildUnit.id).in_(selected_ids),
                )
            ).all()
        }
        route = load_preview_route(
            db, context=context, preview_id=submission.preview_id
        )
        eligible = select(ExecutionStep).where(
            ExecutionStep.tenant_id == context.tenant_id,
            ExecutionStep.submission_id == submission.id,
            col(ExecutionStep.unit_id).in_(selected_ids),
            ExecutionStep.kind == "MATERIAL",
            col(ExecutionStep.status).in_(["PENDING", "QUEUED", "RETRYABLE"]),
            col(ExecutionStep.distribution_id).is_(None),
            col(ExecutionStep.request_body).is_(None),
            col(ExecutionStep.remote_id).is_(None),
            col(ExecutionStep.lease_token).is_(None),
        )
        # 已有可用映射无需进入传输规划，仍由正常步骤准备封面。
        available = select(
            AccountMaterial.material_id, AccountMaterial.advertiser_id
        ).where(
            AccountMaterial.tenant_id == context.tenant_id,
            AccountMaterial.bc_id == submission.bc_id,
            AccountMaterial.connection_id == route.connection_id,
            AccountMaterial.status == "available",
            col(AccountMaterial.verified_at).is_not(None),
        )
        rows = db.exec(
            eligible.join(BuildUnit, col(BuildUnit.id) == ExecutionStep.unit_id)
            .where(
                tuple_(
                    col(ExecutionStep.material_id), col(BuildUnit.advertiser_id)
                ).not_in(available)
            )
            .order_by(col(ExecutionStep.material_id), col(ExecutionStep.id))
            .limit(capacity * 10)
            .with_for_update(of=ExecutionStep)
        ).all()
        ids = sorted({step.material_id for step in rows if step.material_id})[:capacity]
        rows = [step for step in rows if step.material_id in ids]
        if not rows:
            return 0
        db.exec(
            select(MaterialFile)
            .where(
                MaterialFile.tenant_id == context.tenant_id,
                col(MaterialFile.id).in_(ids),
            )
            .order_by(col(MaterialFile.id))
            .with_for_update()
        ).all()
        count = 0
        for unit in units:
            steps = [step for step in rows if step.unit_id == unit.unit_id]
            if not steps:
                continue
            try:
                # 复用已有本地批读：仅当前保存点内缓存权限，退出前重新核验；
                # 所有真实 HTTP 在提交后的 Worker 中重新鉴权，不跨请求缓存。
                with db.begin_nested(), local_read_batch(db):
                    prepared = ensure_target_assets(
                        db,
                        context=context,
                        bc_id=submission.bc_id,
                        material_ids=[
                            step.material_id
                            for step in steps
                            if step.material_id is not None
                        ],
                        advertiser_id=frozen[unit.unit_id].advertiser_id,
                        route=route,
                    )
            except DomainError:
                # 原步骤负责记录权限/内容错误；不得把一个目标失败扩散到其它账户。
                continue
            for step in steps:
                assert step.material_id is not None
                result = prepared[step.material_id]
                if result.state == "queued":
                    step.distribution_id = result.task_id
                    step.status, step.phase, step.error_code = (
                        "PENDING",
                        "IDLE",
                        "material_pending",
                    )
                    step.dispatch_id = None
                    step.updated_at = datetime.now(UTC)
                    count += 1
        return count


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
            route = None
            try:
                route = load_preview_route(
                    session, context=context, preview_id=step.preview_id
                )
                require_same_route(
                    load_material_route(
                        dist.target_route, context=context, bc_id=step.bc_id
                    ),
                    route,
                )
                verify_route(
                    session,
                    context=context,
                    route=route,
                    advertiser_id=dist.advertiser_id,
                    capability="build",
                )
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
                    route=route,
                )
                if (
                    readiness.state != "ready"
                    or not readiness.mapping
                    or readiness.mapping.material_id != step.material_id
                    or readiness.mapping.advertiser_id != frozen.advertiser_id
                    or not readiness.mapping.video_id.strip()
                ):
                    # A historic distribution receipt is not proof of a current
                    # target mapping. Keep uncertainty; never call an upload-capable
                    # ensure helper to recover an ambiguous upload.
                    step.error_code = "target_asset_requires_reconciliation"
                    step.updated_at = datetime.now(UTC)
                    session.add(step)
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
