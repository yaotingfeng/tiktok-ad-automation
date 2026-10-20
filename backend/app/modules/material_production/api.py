from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict

from app.api.deps import CurrentUser, SessionDep

from . import service
from .schemas import ProductionLoginLink, ProductionStatus

router = APIRouter(tags=["material-production"])


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("/tenants/{tenant_id}/material-production", response_model=ProductionStatus)
def get_material_production(
    tenant_id: UUID, session: SessionDep, user: CurrentUser
) -> ProductionStatus:
    return service.production_status(session, tenant_id=tenant_id, actor_id=user.id)


@router.post(
    "/platform/tenants/{tenant_id}/material-production/retry",
    response_model=ProductionStatus,
)
def retry_material_production(
    tenant_id: UUID, session: SessionDep, user: CurrentUser
) -> ProductionStatus:
    result = service.retry_provisioning(session, tenant_id=tenant_id, actor_id=user.id)
    session.commit()
    return result


@router.post(
    "/tenants/{tenant_id}/material-production/login-link",
    response_model=ProductionLoginLink,
)
def post_material_login_link(
    tenant_id: UUID,
    body: EmptyRequest,
    response: Response,
    session: SessionDep,
    user: CurrentUser,
) -> ProductionLoginLink:
    del body
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    result = service.create_login_link(session, tenant_id=tenant_id, actor_id=user.id)
    session.commit()
    return result
