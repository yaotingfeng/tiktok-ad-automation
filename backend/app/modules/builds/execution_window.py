"""按同剧优先推进有界并行组合，避免刷新整个未来图。"""

from uuid import UUID

from sqlalchemy import and_, func, or_, text, tuple_
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.receipt_completion import obsolete_readback_sql

# 与共享接口的账户矩形上限一致；限制活跃图而不提高远端额度/执行槽。
MAX_ACTIVE_UNITS = 10


def window_units():
    step = aliased(ExecutionStep, name="window_step")
    scope = and_(
        col(step.tenant_id) == SubmissionUnit.tenant_id,
        col(step.submission_id) == SubmissionUnit.submission_id,
        col(step.unit_id) == SubmissionUnit.unit_id,
        text("NOT " + obsolete_readback_sql("window_step")),
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
            # 展开尚未完成时看不到完整账户集合，提前发送会退化成1×1共享。
            col(SubmissionUnit.submission_id).in_(
                select(Submission.id).where(Submission.expanded)
            ),
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

    # 集合反连接只扫描依赖一次。逐候选相关EXISTS虽然可执行得快，却使
    # 大库规划成本被放大并触发数秒JIT，耗尽短事务期限。排除NULL保证
    # NOT IN不会把没有绑定步骤的素材库操作错误拦下。
    dependent = select(ExecutionStep.tenant_id, ExecutionStep.cover_job_id).where(
        col(ExecutionStep.cover_job_id).is_not(None)
    )
    # 未绑定搭建步骤的素材库操作不受搭建窗口限制；共享依赖任一当前
    # 组合即准入。批量规划和单任务领取必须复用同一条件，不能先选入
    # 未来成员，再在领取时拒绝它并把当前矩形误判为执行权丢失。
    return and_(
        col(MaterialCoverJob.superseded_by_id).is_(None),
        or_(
            tuple_(col(MaterialCoverJob.tenant_id), col(MaterialCoverJob.id)).not_in(
                dependent
            ),
            col(MaterialCoverJob.id).in_(active_cover_job_ids()),
        ),
    )


def cover_task_admission_condition():
    from app.modules.materials.cover_models import (
        MaterialCoverJob,
        MaterialCoverShareBatch,
    )

    # 历史未发送批次只在任一未完成成员准入时接续；不拆改原成员或发送账本。
    # wake成员自身可能在未来窗口，因此领取与repair必须检查整个批次。
    eligible = (
        select(MaterialCoverJob.id, MaterialCoverJob.share_batch_id)
        .where(col(MaterialCoverJob.status) != "READY", cover_admission_condition())
        .cte("admitted_cover_tasks")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    return and_(
        col(MaterialCoverJob.superseded_by_id).is_(None),
        or_(
            col(MaterialCoverJob.id).in_(select(eligible.c.id)),
            col(MaterialCoverJob.share_batch_id).in_(
                select(eligible.c.share_batch_id).where(
                    eligible.c.share_batch_id.is_not(None)
                )
            ),
            col(MaterialCoverJob.share_batch_id).in_(
                select(MaterialCoverShareBatch.id).where(
                    col(MaterialCoverShareBatch.armed_at).is_not(None)
                )
            ),
        ),
    )


def cover_job_admitted(session: Session, *, tenant_id: UUID, job_id: UUID) -> bool:
    return job_id in cover_jobs_admitted(session, tenant_id=tenant_id, job_ids={job_id})


def cover_jobs_admitted(
    session: Session, *, tenant_id: UUID, job_ids: set[UUID]
) -> set[UUID]:
    """同一领取事务批量复核原窗口条件，避免每成员重算整个执行图。"""
    from app.modules.materials.cover_models import MaterialCoverJob

    return set(
        session.exec(
            select(MaterialCoverJob.id).where(
                MaterialCoverJob.tenant_id == tenant_id,
                col(MaterialCoverJob.id).in_(job_ids),
                cover_task_admission_condition(),
            )
        ).all()
    )
