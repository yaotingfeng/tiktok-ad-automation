"""Tenant-scoped daily plan transactions."""

from datetime import date, datetime
from uuid import UUID

from sqlmodel import Session, col, delete, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.ai_workbench.models import AIOptimizer
from app.modules.ai_workbench.plan_schemas import (
    DailyPlanPage,
    DailyPlanPublic,
    DailyPlanRequest,
    DailyPlanRowInput,
    DailyPlanRowPublic,
    parse_plan_tsv,
    validate_plan_rows,
)
from app.modules.ai_workbench.plans import AIDailyPlan, AIDailyPlanRow
from app.modules.tenants.permissions import require_tenant


def _authorize(session: Session, context: TenantContext, *, write: bool = False) -> None:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="strategy_write" if write else "read",
    )


def _optimizer(session: Session, context: TenantContext, optimizer_id: UUID) -> AIOptimizer:
    row = session.exec(
        select(AIOptimizer).where(
            AIOptimizer.tenant_id == context.tenant_id,
            AIOptimizer.id == optimizer_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError("ai_optimizer_not_found", "ai_optimizer_not_found")
    return row


def _request_rows(body: DailyPlanRequest) -> list[DailyPlanRowInput]:
    try:
        rows = parse_plan_tsv(body.tsv) if body.tsv is not None else list(body.rows or [])
        return validate_plan_rows(rows)
    except (TypeError, ValueError) as error:
        raise DomainError("ai_daily_plan_invalid", str(error)) from error


def _public(session: Session, plan: AIDailyPlan) -> DailyPlanPublic:
    rows = session.exec(
        select(AIDailyPlanRow)
        .where(
            AIDailyPlanRow.tenant_id == plan.tenant_id,
            AIDailyPlanRow.plan_id == plan.id,
        )
        .order_by(col(AIDailyPlanRow.position))
    ).all()
    return DailyPlanPublic(
        id=plan.id,
        tenant_id=plan.tenant_id,
        optimizer_id=plan.optimizer_id,
        business_date=plan.business_date,
        revision=plan.revision,
        rows=[DailyPlanRowPublic.model_validate(row, from_attributes=True) for row in rows],
    )


def _validate_rows(rows: list[DailyPlanRowInput], *, optimizer_id: UUID) -> date:
    if any(row.optimizer_id != optimizer_id for row in rows):
        raise DomainError("ai_daily_plan_optimizer_mismatch", "ai_daily_plan_optimizer_mismatch")
    dates = {row.business_date for row in rows}
    if len(dates) > 1:
        raise DomainError("ai_daily_plan_day_mismatch", "ai_daily_plan_day_mismatch")
    # Empty replacements use the date supplied by the existing parent, so the
    # caller handles this case before invoking this helper.
    if not dates:
        raise DomainError("ai_daily_plan_date_required", "ai_daily_plan_date_required")
    return next(iter(dates))


def create_daily_plan(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    body: DailyPlanRequest,
) -> DailyPlanPublic:
    _authorize(session, context, write=True)
    _optimizer(session, context, optimizer_id)
    rows = _request_rows(body)
    business_date = body.business_date or _validate_rows(
        rows, optimizer_id=optimizer_id
    )
    if rows and _validate_rows(rows, optimizer_id=optimizer_id) != business_date:
        raise DomainError("ai_daily_plan_day_mismatch", "ai_daily_plan_day_mismatch")
    existing = session.exec(
        select(AIDailyPlan).where(
            AIDailyPlan.tenant_id == context.tenant_id,
            AIDailyPlan.optimizer_id == optimizer_id,
            AIDailyPlan.business_date == business_date,
        )
    ).one_or_none()
    if existing is not None:
        raise DomainError("ai_daily_plan_exists", "ai_daily_plan_exists")
    plan = AIDailyPlan(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer_id,
        business_date=business_date,
        created_by=context.actor_id,
        updated_by=context.actor_id,
    )
    session.add(plan)
    session.flush()
    _replace_rows(session, plan, rows)
    session.flush()
    return _public(session, plan)


def update_daily_plan(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    business_date: date | None = None,
    body: DailyPlanRequest,
) -> DailyPlanPublic:
    _authorize(session, context, write=True)
    _optimizer(session, context, optimizer_id)
    rows = _request_rows(body)
    requested_date = business_date or body.business_date
    if requested_date is None and rows:
        requested_date = _validate_rows(rows, optimizer_id=optimizer_id)
    if requested_date is None:
        raise DomainError("ai_daily_plan_date_required", "ai_daily_plan_date_required")
    plan = session.exec(
        select(AIDailyPlan)
        .where(
            AIDailyPlan.tenant_id == context.tenant_id,
            AIDailyPlan.optimizer_id == optimizer_id,
            AIDailyPlan.business_date == requested_date,
        )
        .with_for_update()
    ).one_or_none()
    if plan is None:
        raise DomainError("ai_daily_plan_not_found", "ai_daily_plan_not_found")
    if body.revision != plan.revision:
        raise DomainError("ai_daily_plan_revision_conflict", "ai_daily_plan_revision_conflict")
    if rows and _validate_rows(rows, optimizer_id=optimizer_id) != requested_date:
        raise DomainError("ai_daily_plan_day_mismatch", "ai_daily_plan_day_mismatch")
    plan.revision += 1
    plan.updated_by = context.actor_id
    plan.updated_at = datetime.now(plan.updated_at.tzinfo)
    session.add(plan)
    _replace_rows(session, plan, rows)
    session.flush()
    return _public(session, plan)


def get_daily_plan(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    business_date: date,
) -> DailyPlanPublic:
    _authorize(session, context)
    _optimizer(session, context, optimizer_id)
    plan = session.exec(
        select(AIDailyPlan).where(
            AIDailyPlan.tenant_id == context.tenant_id,
            AIDailyPlan.optimizer_id == optimizer_id,
            AIDailyPlan.business_date == business_date,
        )
    ).one_or_none()
    if plan is None:
        raise DomainError("ai_daily_plan_not_found", "ai_daily_plan_not_found")
    return _public(session, plan)


def list_daily_plans(
    session: Session, *, context: TenantContext, optimizer_id: UUID
) -> DailyPlanPage:
    _authorize(session, context)
    _optimizer(session, context, optimizer_id)
    plans = session.exec(
        select(AIDailyPlan)
        .where(
            AIDailyPlan.tenant_id == context.tenant_id,
            AIDailyPlan.optimizer_id == optimizer_id,
        )
        .order_by(col(AIDailyPlan.business_date).desc())
    ).all()
    return DailyPlanPage(items=[_public(session, plan) for plan in plans], total=len(plans))


def _replace_rows(session: Session, plan: AIDailyPlan, rows: list[DailyPlanRowInput]) -> None:
    # Complete replacement keeps revision semantics simple and prevents stale
    # rows from surviving a user's Excel paste/delete operation.
    session.exec(
        delete(AIDailyPlanRow).where(
            AIDailyPlanRow.tenant_id == plan.tenant_id,
            AIDailyPlanRow.plan_id == plan.id,
        )
    )
    for position, row in enumerate(rows):
        session.add(
            AIDailyPlanRow(
                tenant_id=plan.tenant_id,
                plan_id=plan.id,
                optimizer_id=plan.optimizer_id,
                business_date=plan.business_date,
                scheduled_time=row.scheduled_time,
                title=row.title,
                provider_connection_id=row.provider_connection_id,
                application_id=row.application_id,
                promotion_url=row.promotion_url,
                external_drama_id=row.external_drama_id,
                order_materials=row.order_materials,
                position=position,
            )
        )

