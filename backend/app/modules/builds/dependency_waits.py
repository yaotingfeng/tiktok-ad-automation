"""素材依赖落定才唤醒正常执行器，不为等待本身制造轮询消息。"""

from typing import Any

from sqlalchemy import and_, func, or_, tuple_
from sqlalchemy import select as sa_select
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_window import material_unit_admitted, window_units
from app.modules.builds.preview_models import BuildUnit
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial, MaterialDistribution


def waiting_dependency(step: ExecutionStep) -> bool:
    return (
        step.kind == "MATERIAL"
        and step.status == "PENDING"
        and (
            step.error_code == "execution_window_wait"
            or (
                step.error_code == "material_pending"
                and step.distribution_id is not None
            )
            or (step.error_code == "cover_pending" and step.cover_job_id is not None)
        )
    )


def wake_material_dependencies(*, database_engine: Any, limit: int = 100) -> int:
    from app.modules.builds.dispatch import queue_step
    from app.modules.builds.material_execution import (
        material_needs_planning,
        plan_material_slice,
    )

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid dependency recovery page")
    admitted = window_units().cte("wake_admitted_units")
    active_slices = (
        select(ExecutionStep.tenant_id, ExecutionStep.submission_id)
        .join(
            MaterialDistribution,
            and_(
                col(MaterialDistribution.tenant_id) == ExecutionStep.tenant_id,
                col(MaterialDistribution.id) == ExecutionStep.distribution_id,
            ),
        )
        .where(
            tuple_(
                col(ExecutionStep.tenant_id),
                col(ExecutionStep.submission_id),
                col(ExecutionStep.unit_id),
            ).in_(select(admitted)),
            col(MaterialDistribution.status).in_(["queued", "preparing", "verifying"]),
        )
        .group_by(col(ExecutionStep.tenant_id), col(ExecutionStep.submission_id))
        .having(func.count(col(ExecutionStep.material_id).distinct()) >= 20)
    )
    mapped = (
        select(AccountMaterial.id)
        .where(
            AccountMaterial.tenant_id == ExecutionStep.tenant_id,
            AccountMaterial.bc_id == ExecutionStep.bc_id,
            AccountMaterial.material_id == ExecutionStep.material_id,
            AccountMaterial.advertiser_id == BuildUnit.advertiser_id,
            AccountMaterial.connection_id == BuildUnit.connection_id,
            AccountMaterial.status == "available",
            col(AccountMaterial.verified_at).is_not(None),
        )
        .exists()
    )
    with Session(database_engine) as session:
        candidates = (
            session.connection()
            .execute(
                sa_select(
                    col(ExecutionStep.tenant_id),
                    col(ExecutionStep.unit_id),
                    col(ExecutionStep.id),
                    col(ExecutionStep.submission_id),
                    col(Submission.actor_id),
                    col(ExecutionStep.error_code),
                )
                .join(
                    Submission,
                    and_(
                        col(Submission.tenant_id) == ExecutionStep.tenant_id,
                        col(Submission.id) == ExecutionStep.submission_id,
                    ),
                )
                .join(
                    BuildUnit,
                    and_(
                        col(BuildUnit.tenant_id) == ExecutionStep.tenant_id,
                        col(BuildUnit.id) == ExecutionStep.unit_id,
                    ),
                )
                .outerjoin(
                    MaterialDistribution,
                    and_(
                        col(MaterialDistribution.id) == ExecutionStep.distribution_id,
                        col(MaterialDistribution.tenant_id) == ExecutionStep.tenant_id,
                    ),
                )
                .outerjoin(
                    MaterialCoverJob,
                    and_(
                        col(MaterialCoverJob.id) == ExecutionStep.cover_job_id,
                        col(MaterialCoverJob.tenant_id) == ExecutionStep.tenant_id,
                    ),
                )
                .where(
                    col(ExecutionStep.kind) == "MATERIAL",
                    col(ExecutionStep.status) == "PENDING",
                    col(ExecutionStep.dispatch_id).is_(None),
                    tuple_(
                        col(ExecutionStep.tenant_id),
                        col(ExecutionStep.submission_id),
                        col(ExecutionStep.unit_id),
                    ).in_(select(admitted)),
                    or_(
                        and_(
                            col(ExecutionStep.error_code) == "execution_window_wait",
                            # 先在SQL中过滤未来片，不能占走有界页后再逐条丢弃，
                            # 否则已落定依赖会被更旧的等待行持续饿死。
                            or_(
                                tuple_(
                                    col(ExecutionStep.tenant_id),
                                    col(ExecutionStep.submission_id),
                                ).not_in(active_slices),
                                col(ExecutionStep.distribution_id).is_not(None),
                                col(ExecutionStep.cover_job_id).is_not(None),
                                mapped,
                            ),
                        ),
                        and_(
                            col(ExecutionStep.error_code) == "material_pending",
                            col(MaterialDistribution.status).in_(
                                ["ready", "blocked", "result_unknown"]
                            ),
                        ),
                        and_(
                            col(ExecutionStep.error_code) == "cover_pending",
                            col(MaterialCoverJob.status).in_(
                                ["READY", "BLOCKED", "UNKNOWN"]
                            ),
                        ),
                    ),
                )
                .order_by(col(ExecutionStep.updated_at), col(ExecutionStep.id))
                .limit(limit)
            )
            .all()
        )
    # 规划需按统一顺序锁整个账户窗口，因此必须在下方单元/步骤锁外执行。
    # 同一提交一轮仅尝试一次；领取冲突或仍有活动片时保留等待，不制造消息。
    planned = set()
    for tenant_id, unit_id, _, submission_id, actor_id, error_code in candidates:
        identity = (tenant_id, submission_id)
        if error_code != "execution_window_wait" or identity in planned:
            continue
        planned.add(identity)
        try:
            plan_material_slice(
                database_engine=database_engine,
                context=TenantContext(
                    tenant_id=tenant_id, actor_id=actor_id, role="operator"
                ),
                unit_id=unit_id,
            )
        except DomainError:
            # 原执行器仍负责记录权限/冻结错误；本轮不能吞掉正式失败路径。
            pass
    count = 0
    for tenant_id, unit_id, step_id, _, _, _ in candidates:
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
                    ExecutionStep.tenant_id == tenant_id,
                    ExecutionStep.id == step_id,
                )
                .with_for_update()
            ).one()
            if not waiting_dependency(step) or step.dispatch_id is not None:
                continue
            if not material_unit_admitted(
                session,
                tenant_id=tenant_id,
                submission_id=step.submission_id,
                unit_id=unit_id,
            ):
                continue
            row = session.get(Submission, step.submission_id)
            assert row
            if step.error_code == "execution_window_wait":
                if material_needs_planning(
                    session,
                    TenantContext(
                        tenant_id=tenant_id, actor_id=row.actor_id, role="operator"
                    ),
                    step,
                ):
                    continue
                queue_step(session, step=step, submission=row)
                count += 1
                continue
            dependency = (
                session.get(MaterialCoverJob, step.cover_job_id)
                if step.error_code == "cover_pending"
                else session.get(MaterialDistribution, step.distribution_id)
            )
            frozen = session.get(BuildUnit, unit_id)
            if (
                dependency is None
                or frozen is None
                or (
                    dependency.tenant_id,
                    dependency.bc_id,
                    dependency.material_id,
                    dependency.advertiser_id,
                )
                != (step.tenant_id, step.bc_id, step.material_id, frozen.advertiser_id)
            ):
                continue
            if dependency.status not in {
                "ready",
                "blocked",
                "result_unknown",
                "READY",
                "BLOCKED",
                "UNKNOWN",
            }:
                continue
            # 这里只恢复投递，不把依赖回执冒充步骤成功。权限、冻结路由、
            # 视频/封面新鲜度及 UNKNOWN 禁止重传仍由原执行器完整验证。
            queue_step(session, step=step, submission=row)
            count += 1
    return count
