"""按同剧优先推进有界并行组合，避免刷新整个未来图。"""

from uuid import UUID

from sqlalchemy import and_, func, or_, tuple_
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.modules.builds.execution_models import ExecutionStep, SubmissionUnit
from app.modules.builds.preview_models import BuildUnit

# 与共享接口的账户矩形上限一致；限制活跃图而不提高远端额度/执行槽。
MAX_ACTIVE_UNITS = 10


def window_units():
    step = aliased(ExecutionStep)
    scope = and_(
        col(step.tenant_id) == SubmissionUnit.tenant_id,
        col(step.submission_id) == SubmissionUnit.submission_id,
        col(step.unit_id) == SubmissionUnit.unit_id,
    )
    unfinished = select(step.id).where(scope, col(step.status) != "SUCCEEDED").exists()
    blocked = (
        select(step.id)
        .where(
            scope,
            or_(
                col(step.status) == "UNKNOWN",
                and_(
                    col(step.status) == "FAILED",
                    col(step.error_code).is_distinct_from("dependency_failed"),
                ),
            ),
        )
        .exists()
    )
    # UNKNOWN/明确失败不占住其他组合的窗口；原恢复链路仍保留，核实后
    # 自动重新参与排序。已完成组合自然退出，不改冻结意图或业务结果。
    ranked = (
        select(
            SubmissionUnit.tenant_id,
            SubmissionUnit.submission_id,
            SubmissionUnit.unit_id,
            func.row_number()
            .over(
                partition_by=(
                    col(SubmissionUnit.tenant_id),
                    col(SubmissionUnit.submission_id),
                ),
                order_by=(col(BuildUnit.drama_id), col(SubmissionUnit.unit_id)),
            )
            .label("position"),
        )
        .join(
            BuildUnit,
            and_(
                col(BuildUnit.tenant_id) == SubmissionUnit.tenant_id,
                col(BuildUnit.id) == SubmissionUnit.unit_id,
            ),
        )
        .where(
            SubmissionUnit.expanded,
            SubmissionUnit.disposition == "INCLUDED",
            unfinished,
            ~blocked,
        )
        .subquery()
    )
    return (
        select(
            SubmissionUnit.tenant_id,
            SubmissionUnit.submission_id,
            SubmissionUnit.unit_id,
        )
        .where(
            tuple_(
                col(SubmissionUnit.tenant_id),
                col(SubmissionUnit.submission_id),
                col(SubmissionUnit.unit_id),
            ).in_(
                select(
                    ranked.c.tenant_id, ranked.c.submission_id, ranked.c.unit_id
                ).where(ranked.c.position <= MAX_ACTIVE_UNITS)
            )
        )
        .order_by(
            col(SubmissionUnit.tenant_id),
            col(SubmissionUnit.submission_id),
            col(SubmissionUnit.unit_id),
        )
    )


def material_unit_admitted(
    session: Session, *, tenant_id: UUID, submission_id: UUID, unit_id: UUID
) -> bool:
    selected = session.exec(
        window_units().where(
            SubmissionUnit.tenant_id == tenant_id,
            SubmissionUnit.submission_id == submission_id,
            SubmissionUnit.unit_id == unit_id,
        )
    ).first()
    return selected is not None


def active_cover_job_ids():
    return select(ExecutionStep.cover_job_id).where(
        col(ExecutionStep.cover_job_id).is_not(None),
        tuple_(
            col(ExecutionStep.tenant_id),
            col(ExecutionStep.submission_id),
            col(ExecutionStep.unit_id),
        ).in_(window_units()),
    )


def cover_admission_condition():
    from app.modules.materials.cover_models import MaterialCoverJob

    dependent = (
        select(ExecutionStep.id)
        .where(
            ExecutionStep.tenant_id == MaterialCoverJob.tenant_id,
            ExecutionStep.cover_job_id == MaterialCoverJob.id,
        )
        .correlate(MaterialCoverJob)
        .exists()
    )
    # 未绑定搭建步骤的素材库操作不受搭建窗口限制；共享依赖任一当前
    # 组合即准入。批量规划和单任务领取必须复用同一条件，不能先选入
    # 未来成员，再在领取时拒绝它并把当前矩形误判为执行权丢失。
    return or_(~dependent, col(MaterialCoverJob.id).in_(active_cover_job_ids()))


def cover_job_admitted(session: Session, *, tenant_id: UUID, job_id: UUID) -> bool:
    from app.modules.materials.cover_models import MaterialCoverJob

    return (
        session.exec(
            select(MaterialCoverJob.id).where(
                MaterialCoverJob.tenant_id == tenant_id,
                MaterialCoverJob.id == job_id,
                cover_admission_condition(),
            )
        ).first()
        is not None
    )
