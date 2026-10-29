"""轮播库 API：租户来自路径/登录用户；保存和加载仍绑定已授权草稿。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import Field

from app.api.deps import CurrentUser, SessionDep
from app.modules.builds.image_composition_api import Change, _scope
from app.modules.builds.image_composition_schemas import ImmutableModel
from app.modules.materials import carousel_presets as service
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/carousel-presets")


class DraftChange(Change):
    draft_id: UUID


class Save(DraftChange):
    source_unit_id: UUID
    name: str = Field(min_length=1, max_length=200)


class Copy(ImmutableModel):
    request_id: UUID
    name: str = Field(min_length=1, max_length=200)


class Load(DraftChange):
    drama_id: UUID
    allow_reuse: bool = Field(default=False, strict=True)


@router.get("")
def list_versions(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    name: Annotated[str, Query(max_length=200)] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.list_preset_versions(
        session, context, name=name, offset=offset, limit=limit
    )


@router.get("/{version_id}")
def read_version(
    tenant_id: UUID, version_id: UUID, session: SessionDep, user: CurrentUser
):
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.get_preset_version(session, context, version_id)


@router.post("")
def save_version(tenant_id: UUID, body: Save, session: SessionDep, user: CurrentUser):
    scope = _scope(session, user, tenant_id, body.draft_id, write=True)
    result = service.save_preset_version(
        session,
        scope,
        body.source_unit_id,
        body.expected_revision,
        body.name,
        body.request_id,
    )
    session.commit()
    return result


@router.post("/{version_id}/copy")
def copy_version(
    tenant_id: UUID,
    version_id: UUID,
    body: Copy,
    session: SessionDep,
    user: CurrentUser,
):
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    result = service.copy_preset_version(
        session, context, version_id, body.name, body.request_id
    )
    session.commit()
    return result


@router.post("/{version_id}/load")
def load_version(
    tenant_id: UUID,
    version_id: UUID,
    body: Load,
    session: SessionDep,
    user: CurrentUser,
):
    scope = _scope(session, user, tenant_id, body.draft_id, write=True)
    result = service.load_preset_into_draft(
        session,
        scope,
        version_id,
        body.request_id,
        body.expected_revision,
        drama_id=body.drama_id,
        allow_reuse=body.allow_reuse,
    )
    session.commit()
    return result
