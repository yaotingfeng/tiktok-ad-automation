"""HTTP contracts for tenant AI optimizer configuration."""

from uuid import UUID

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.core.context import TenantContext
from app.modules.ai_workbench import service
from app.modules.ai_workbench.schemas import (
    OptimizerCreate,
    OptimizerPage,
    OptimizerPublic,
    OptimizerUpdate,
)

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["ai_workbench"])


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
