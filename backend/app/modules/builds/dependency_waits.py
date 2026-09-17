"""素材依赖落定才唤醒正常执行器，不为等待本身制造轮询消息。"""

from typing import Any

from sqlalchemy import and_, or_, tuple_
from sqlmodel import Session, col, select

from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_window import material_unit_admitted, window_units
from app.modules.builds.preview_models import BuildUnit
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import MaterialDistribution


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

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid dependency recovery page")
    with Session(database_engine) as session:
        candidates = session.exec(
            select(ExecutionStep.tenant_id, ExecutionStep.unit_id, ExecutionStep.id)
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
                ExecutionStep.kind == "MATERIAL",
                ExecutionStep.status == "PENDING",
                col(ExecutionStep.dispatch_id).is_(None),
                tuple_(
                    col(ExecutionStep.tenant_id),
                    col(ExecutionStep.submission_id),
                    col(ExecutionStep.unit_id),
                ).in_(window_units()),
                or_(
                    col(ExecutionStep.error_code) == "execution_window_wait",
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
        ).all()
    count = 0
    for tenant_id, unit_id, step_id in candidates:
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
