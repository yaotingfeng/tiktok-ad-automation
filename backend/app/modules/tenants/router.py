from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.core.pagination import Page
from app.models import User
from app.modules.tenants import service
from app.modules.tenants.models import Tenant
from app.modules.tenants.permissions import require_tenant
from app.modules.tenants.schemas import (
    MemberPublic,
    MemberRole,
    MemberSet,
    TenantCreate,
    TenantSummary,
    TenantUpdate,
)

router = APIRouter(tags=["tenants"])
PageLimit = Annotated[int, Query(ge=1, le=200)]
Search = Annotated[str, Query(max_length=255)]


@router.get("/me/tenants", response_model=Page[TenantSummary])
def get_my_tenants(
    session: SessionDep,
    user: CurrentUser,
    after_id: UUID | None = None,
    limit: PageLimit = 50,
    search: Search = "",
    active: bool | None = None,
) -> Page[TenantSummary]:
    return service.list_tenants(
        session,
        actor_id=user.id,
        after_id=after_id,
        limit=limit,
        search=search,
        active=active,
    )


@router.get("/platform/tenants", response_model=Page[TenantSummary])
def get_platform_tenants(
    session: SessionDep,
    user: CurrentUser,
    after_id: UUID | None = None,
    limit: PageLimit = 50,
    search: Search = "",
    active: bool | None = None,
) -> Page[TenantSummary]:
    return service.list_tenants(
        session,
        actor_id=user.id,
        platform_only=True,
        after_id=after_id,
        limit=limit,
        search=search,
        active=active,
    )


@router.post("/platform/tenants", response_model=Tenant, status_code=201)
def post_tenant(body: TenantCreate, session: SessionDep, user: CurrentUser) -> Tenant:
    result = service.create_tenant(session, actor_id=user.id, **body.model_dump())
    session.commit()
    session.refresh(result)
    return result


@router.patch("/platform/tenants/{tenant_id}", response_model=Tenant)
def patch_tenant(
    tenant_id: UUID, body: TenantUpdate, session: SessionDep, user: CurrentUser
) -> Tenant:
    result = service.update_tenant(
        session,
        actor_id=user.id,
        tenant_id=tenant_id,
        changes=body.model_dump(exclude_unset=True),
    )
    session.commit()
    session.refresh(result)
    return result


@router.get("/tenants/{tenant_id}/members", response_model=Page[MemberPublic])
def get_members(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    after_id: UUID | None = None,
    limit: PageLimit = 50,
    search: Search = "",
    role: MemberRole | None = None,
    active: bool | None = None,
) -> Page[MemberPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    return service.list_members(
        session,
        context=context,
        after_id=after_id,
        limit=limit,
        search=search,
        role=role,
        active=active,
    )


@router.put("/tenants/{tenant_id}/members", response_model=MemberPublic)
def put_member(
    tenant_id: UUID, body: MemberSet, session: SessionDep, user: CurrentUser
) -> MemberPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    result = service.set_member(session, context=context, **body.model_dump())
    target = session.get(User, result.user_id)
    assert target is not None
    response = service.member_public(result, target)
    session.commit()
    return response
