"""独立复制工作区，GET只返回本地持久事实。"""

from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.modules.campaign_copies import service
from app.modules.campaign_copies.schemas import (
    CopyCreateRequest,
    CopyDraftPublic,
    CopyPatchRequest,
    CopyRevisionRequest,
    CopySubmitRequest,
)
from app.modules.tenants.permissions import require_tenant

router = APIRouter(
    prefix="/tenants/{tenant_id}/campaign-copies", tags=["campaign_copies"]
)


@router.post(
    "",
    response_model=CopyDraftPublic,
    status_code=201,
    operation_id="campaign_copies-create",
)
def create(
    tenant_id: UUID, body: CopyCreateRequest, session: SessionDep, user: CurrentUser
) -> CopyDraftPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    row = service.create(session, context=context, **body.model_dump())
    result = service.public(row)
    session.commit()
    return result


@router.get(
    "/{draft_id}", response_model=CopyDraftPublic, operation_id="campaign_copies-get"
)
def get(
    tenant_id: UUID,
    draft_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    bc_id: str = Query(min_length=1, max_length=128),
) -> CopyDraftPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    row = service.get_draft(session, context=context, draft_id=draft_id)
    if row.bc_id != bc_id:
        from app.core.errors import DomainError

        raise DomainError("copy_not_found", "复制草稿不属于当前 BC")
    return service.public(row)


@router.patch(
    "/{draft_id}", response_model=CopyDraftPublic, operation_id="campaign_copies-patch"
)
def patch(
    tenant_id: UUID,
    draft_id: UUID,
    body: CopyPatchRequest,
    session: SessionDep,
    user: CurrentUser,
) -> CopyDraftPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    result = service.public(
        service.patch(session, context=context, draft_id=draft_id, body=body)
    )
    session.commit()
    return result


@router.post(
    "/{draft_id}/preview",
    response_model=CopyDraftPublic,
    status_code=202,
    operation_id="campaign_copies-preview",
)
def preview(
    tenant_id: UUID,
    draft_id: UUID,
    body: CopyRevisionRequest,
    session: SessionDep,
    user: CurrentUser,
) -> CopyDraftPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    result = service.public(
        service.preview(session, context=context, draft_id=draft_id, body=body)
    )
    session.commit()
    return result


@router.post(
    "/{draft_id}/submit",
    response_model=CopyDraftPublic,
    status_code=202,
    operation_id="campaign_copies-submit",
)
def submit(
    tenant_id: UUID,
    draft_id: UUID,
    body: CopySubmitRequest,
    session: SessionDep,
    user: CurrentUser,
) -> CopyDraftPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    result = service.public(
        service.submit(session, context=context, draft_id=draft_id, body=body)
    )
    session.commit()
    return result
