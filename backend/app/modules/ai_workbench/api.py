"""HTTP contracts for tenant AI optimizer configuration."""

from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep
from app.core.context import TenantContext
from app.modules.ai_workbench import (
    daily_report,
    plans_service,
    proposals,
    reviews,
    service,
)
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


class ReviewResponse(BaseModel):
    task: dict
    proposals: list[dict]


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
    result = service.create_optimizer(
        session, context=_context(user, tenant_id), body=body
    )
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
        return plans_service.list_daily_plans(
            session, context=context, optimizer_id=optimizer_id
        )
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


@router.get("/ai-optimizers/{optimizer_id}/occurrences", response_model=list[dict])
def list_occurrences(
    tenant_id: UUID,
    optimizer_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """Return tenant/optimizer-scoped handoff and readback evidence for the workbench."""
    context = _context(user, tenant_id)
    service.get_optimizer(session, context=context, optimizer_id=optimizer_id)
    statement = (
        select(BuildOccurrence)
        .where(
            BuildOccurrence.tenant_id == tenant_id,
            BuildOccurrence.optimizer_id == optimizer_id,
        )
        .order_by(BuildOccurrence.scheduled_at.desc(), BuildOccurrence.id.desc())
        .limit(limit)
    )
    if status:
        statement = statement.where(BuildOccurrence.status == status)
    rows = session.exec(statement).all()
    return [row.model_dump(mode="json") for row in rows]


@router.get("/ai-optimizers/{optimizer_id}/daily-report")
def get_daily_report(
    tenant_id: UUID,
    optimizer_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    business_date: date | None = Query(default=None),
) -> dict:
    """Return the read-only daily summary built from frozen workbench evidence."""
    if business_date is None:
        optimizer = service.get_optimizer(
            session, context=_context(user, tenant_id), optimizer_id=optimizer_id
        )
        business_date = datetime.now(ZoneInfo(optimizer.timezone)).date()
    return daily_report.build_daily_report(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        business_date=business_date,
    )


@router.post(
    "/ai-optimizers/{optimizer_id}/reviews",
    response_model=ReviewResponse,
    status_code=201,
)
def create_review(
    tenant_id: UUID,
    optimizer_id: UUID,
    body: proposals.ReviewRequest,
    session: SessionDep,
    user: CurrentUser,
) -> ReviewResponse:
    context = _context(user, tenant_id)
    task = reviews.create_review_task(
        session,
        context=context,
        optimizer_id=optimizer_id,
        request_id=body.request_id,
        evidence_ids=body.evidence_ids,
        knowledge_refs=body.knowledge_refs,
        authorization_ttl_seconds=body.authorization_ttl_seconds,
        dsh_task_id=body.dsh_task_id,
        request_digest=__import__("hashlib")
        .sha256(
            __import__("json")
            .dumps(body.model_dump(mode="json", exclude={"request_id"}), sort_keys=True)
            .encode()
        )
        .hexdigest(),
    )
    rows: list[proposals.AIProposal] = []
    for index, change in enumerate(body.proposals):
        # One review request may contain several changes; derive stable child
        # request IDs so retries cannot create duplicate proposals.
        child_request = UUID(int=(body.request_id.int + index + 1) % (1 << 128))
        rows.append(
            proposals.create_proposal(
                session,
                context=context,
                optimizer_id=optimizer_id,
                review_task=task,
                change=change,
                request_id=child_request,
            )
        )
    proposals.publish_auto_proposals(
        session,
        context=context,
        optimizer_id=optimizer_id,
        review_task_id=task.id,
    )
    session.commit()
    return ReviewResponse(
        task={
            "id": task.id,
            "tenant_id": task.tenant_id,
            "optimizer_id": task.optimizer_id,
            "dsh_task_id": task.dsh_task_id,
            "status": task.status,
            "fence": task.fence,
            "restart_generation": task.restart_generation,
            "optimizer_version": task.optimizer_version,
            "authorization_expires_at": task.authorization_expires_at,
            "evidence": task.evidence,
            "result": task.result,
        },
        proposals=[proposals._public(row) for row in rows],
    )


@router.get("/ai-optimizers/{optimizer_id}/proposals")
def get_proposals(
    tenant_id: UUID,
    optimizer_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    status: str | None = Query(default=None),
) -> list[dict]:
    return proposals.list_proposals(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        status=status,
    )


@router.post("/ai-optimizers/{optimizer_id}/proposals/{proposal_id}/accept")
def accept_proposal(
    tenant_id: UUID,
    optimizer_id: UUID,
    proposal_id: UUID,
    _body: proposals.DecisionRequest,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    row = proposals.accept_proposal(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        proposal_id=proposal_id,
        request_id=_body.request_id,
    )
    session.commit()
    return proposals._public(row)


@router.post("/ai-optimizers/{optimizer_id}/proposals/{proposal_id}/reject")
def reject_proposal(
    tenant_id: UUID,
    optimizer_id: UUID,
    proposal_id: UUID,
    body: proposals.DecisionRequest,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    row = proposals.reject_proposal(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        proposal_id=proposal_id,
        reason=body.reason,
        request_id=body.request_id,
    )
    session.commit()
    return proposals._public(row)


@router.get("/ai-optimizers/{optimizer_id}/reviews")
def list_reviews(
    tenant_id: UUID, optimizer_id: UUID, session: SessionDep, user: CurrentUser
) -> dict:
    from app.core.config import settings

    service.get_optimizer(
        session, context=_context(user, tenant_id), optimizer_id=optimizer_id
    )
    tasks = session.exec(
        select(reviews.DSHReviewTask)
        .where(
            reviews.DSHReviewTask.tenant_id == tenant_id,
            reviews.DSHReviewTask.optimizer_id == optimizer_id,
        )
        .order_by(reviews.DSHReviewTask.created_at.desc())
        .limit(50)
    ).all()
    return {
        "runtime_ready": bool(settings.AI_DSH_SERVICE_TOKEN),
        "items": [
            {
                "id": task.id,
                "status": task.status,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
                "optimizer_version": task.optimizer_version,
                "result": task.result,
            }
            for task in tasks
        ],
    }


class ReviewControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/ai-optimizers/{optimizer_id}/reviews/{task_id}/cancel")
def cancel_review(
    tenant_id: UUID,
    optimizer_id: UUID,
    task_id: UUID,
    _body: ReviewControlRequest,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    from app.modules.ai_workbench import ai_tasks

    context = _context(user, tenant_id)
    task = reviews.get_review_task(
        session, context=context, optimizer_id=optimizer_id, task_id=task_id
    )
    task = ai_tasks.cancel_task(
        session,
        context=context,
        optimizer_id=optimizer_id,
        task_id=task_id,
        fence=task.fence,
    )
    session.commit()
    return {"id": task.id, "status": task.status}


@router.post("/ai-optimizers/{optimizer_id}/reviews/{task_id}/retry")
def retry_review(
    tenant_id: UUID,
    optimizer_id: UUID,
    task_id: UUID,
    _body: ReviewControlRequest,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    from app.modules.ai_workbench import ai_tasks

    context = _context(user, tenant_id)
    task = ai_tasks.restart_task(
        session, context=context, optimizer_id=optimizer_id, task_id=task_id
    )
    session.commit()
    return {"id": task.id, "status": task.status}


@router.get("/ai-optimizers/{optimizer_id}/reviews/{task_id}")
def get_review(
    tenant_id: UUID,
    optimizer_id: UUID,
    task_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> dict:
    task = reviews.get_review_task(
        session,
        context=_context(user, tenant_id),
        optimizer_id=optimizer_id,
        task_id=task_id,
    )
    return {
        "id": task.id,
        "status": task.status,
        "result": task.result,
        "error_message": task.result.get("summary")
        if task.status == "FAILED"
        else None,
    }


@router.get("/ai-material-options")
def material_options(tenant_id: UUID, session: SessionDep, user: CurrentUser) -> dict:
    from app.core.errors import DomainError
    from app.modules.ai_workbench.orders import company_client

    try:
        client = company_client(session, context=_context(user, tenant_id))
    except DomainError as error:
        if error.code == "material_production_not_ready":
            return {"ready": False, "strategies": []}
        raise
    catalog = client.request("GET", "/api/strategy-catalog")
    return {
        "ready": True,
        "strategies": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "enabled": item.get("enabled") is True,
            }
            for item in catalog.get("strategies", [])
            if isinstance(item, dict)
        ],
    }
