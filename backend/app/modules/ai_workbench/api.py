"""HTTP contracts for tenant AI optimizer configuration."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep
from app.core.context import TenantContext
from app.modules.ai_workbench import plans_service, service
from app.modules.ai_workbench.build_handoff import BuildHandoff
from app.modules.ai_workbench.models import BuildOccurrence
from app.modules.ai_workbench.plan_schemas import (
    DailyPlanPage,
    DailyPlanPublic,
    DailyPlanRequest,
)
from app.modules.ai_workbench.plans import AIDailyPlanRow
from app.modules.ai_workbench.schemas import (
    OptimizerCreate,
    OptimizerPage,
    OptimizerPublic,
    OptimizerUpdate,
)

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["ai_workbench"])


class RunPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    strategy_version_id: UUID | None = None


def _context(user: CurrentUser, tenant_id: UUID) -> TenantContext:
    # 权限由 service 每次读写重新从租户成员关系核验；这里不信任客户端角色。
    return TenantContext(tenant_id=tenant_id, actor_id=user.id, role="")


@router.get("/ai-optimizers", response_model=OptimizerPage)
def list_optimizers(
    tenant_id: UUID, session: SessionDep, user: CurrentUser
) -> OptimizerPage:
    return service.list_optimizers(session, context=_context(user, tenant_id))


@router.post("/ai-optimizers", response_model=OptimizerPublic, status_code=201)
def create_optimizer(
    tenant_id: UUID,
    body: OptimizerCreate,
    session: SessionDep,
    user: CurrentUser,
) -> OptimizerPublic:
    result = service.create_optimizer(session, context=_context(user, tenant_id), body=body)
    session.commit()
    return result


@router.get("/ai-optimizers/{optimizer_id}", response_model=OptimizerPublic)
def get_optimizer(
    tenant_id: UUID, optimizer_id: UUID, session: SessionDep, user: CurrentUser
) -> OptimizerPublic:
    return service.get_optimizer(
        session, context=_context(user, tenant_id), optimizer_id=optimizer_id
    )


@router.patch("/ai-optimizers/{optimizer_id}", response_model=OptimizerPublic)
def update_optimizer(
    tenant_id: UUID,
    optimizer_id: UUID,
    body: OptimizerUpdate,
    session: SessionDep,
    user: CurrentUser,
) -> OptimizerPublic:
    result = service.update_optimizer(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        body=body,
    )
    session.commit()
    return result


@router.get(
    "/ai-optimizers/{optimizer_id}/daily-plans",
    response_model=DailyPlanPublic | DailyPlanPage,
)
def get_daily_plans(
    tenant_id: UUID,
    optimizer_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    business_date: date | None = Query(default=None),
) -> DailyPlanPublic | DailyPlanPage:
    context = _context(user, tenant_id)
    if business_date is None:
        return plans_service.list_daily_plans(session, context=context, optimizer_id=optimizer_id)
    return plans_service.get_daily_plan(
        session, context=context, optimizer_id=optimizer_id, business_date=business_date
    )


@router.post(
    "/ai-optimizers/{optimizer_id}/daily-plans",
    response_model=DailyPlanPublic,
    status_code=201,
)
def create_daily_plan(
    tenant_id: UUID,
    optimizer_id: UUID,
    body: DailyPlanRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DailyPlanPublic:
    result = plans_service.create_daily_plan(
        session, context=_context(user, tenant_id), optimizer_id=optimizer_id, body=body
    )
    session.commit()
    return result


@router.patch(
    "/ai-optimizers/{optimizer_id}/daily-plans",
    response_model=DailyPlanPublic,
)
def update_daily_plan(
    tenant_id: UUID,
    optimizer_id: UUID,
    body: DailyPlanRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DailyPlanPublic:
    result = plans_service.update_daily_plan(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        body=body,
    )
    session.commit()
    return result


@router.post(
    "/ai-optimizers/{optimizer_id}/plans/{plan_id}/run",
    response_model=BuildOccurrence,
    status_code=202,
)
def run_daily_plan_row(
    tenant_id: UUID,
    optimizer_id: UUID,
    plan_id: UUID,
    body: RunPlanRequest,
    session: SessionDep,
    user: CurrentUser,
) -> BuildOccurrence:
    """Create a dry local handoff; the durable outbox performs the build later."""
    context = _context(user, tenant_id)
    # The row itself carries optimizer ownership.  Resolve and check the path
    # optimizer before any outbox write so a copied URL cannot cross scopes.
    row = session.exec(
        select(AIDailyPlanRow).where(
            AIDailyPlanRow.tenant_id == tenant_id,
            AIDailyPlanRow.id == plan_id,
            AIDailyPlanRow.optimizer_id == optimizer_id,
        )
    ).one_or_none()
    if row is None:
        from app.core.errors import DomainError

        raise DomainError("ai_daily_plan_row_not_found", "ai_daily_plan_row_not_found")
    occurrence = BuildHandoff(session, context=context).create_occurrence(
        plan_id,
        body.request_id,
        strategy_version_id=body.strategy_version_id,
    )
    session.commit()
    return occurrence
