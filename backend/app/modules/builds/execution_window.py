"""每份提交只推进一个可执行组合的素材，避免刷新整个未来图。"""

from uuid import UUID

from sqlalchemy import and_, or_, tuple_
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.modules.builds.execution_models import ExecutionStep, SubmissionUnit


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
    return (
        select(
            SubmissionUnit.tenant_id,
            SubmissionUnit.submission_id,
            SubmissionUnit.unit_id,
        )
        .where(
            SubmissionUnit.expanded,
            SubmissionUnit.disposition == "INCLUDED",
            unfinished,
            ~blocked,
        )
        .distinct(col(SubmissionUnit.tenant_id), col(SubmissionUnit.submission_id))
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
        )
    ).first()
    return selected is not None and selected[2] == unit_id


def active_cover_job_ids():
    return select(ExecutionStep.cover_job_id).where(
        col(ExecutionStep.cover_job_id).is_not(None),
        tuple_(
            col(ExecutionStep.tenant_id),
            col(ExecutionStep.submission_id),
            col(ExecutionStep.unit_id),
        ).in_(window_units()),
    )


def cover_job_admitted(session: Session, *, tenant_id: UUID, job_id: UUID) -> bool:
    dependent = session.exec(
        select(ExecutionStep.id)
        .where(
            ExecutionStep.tenant_id == tenant_id,
            ExecutionStep.cover_job_id == job_id,
        )
        .limit(1)
    ).first()
    # 未绑定搭建步骤的素材库操作不受搭建窗口限制；共享依赖任一当前
    # 组合即准入，不根据某一个历史消费者擅自阻断其他使用者。
    return (
        dependent is None
        or session.exec(
            active_cover_job_ids()
            .where(
                ExecutionStep.tenant_id == tenant_id,
                ExecutionStep.cover_job_id == job_id,
            )
            .limit(1)
        ).first()
        is not None
    )
