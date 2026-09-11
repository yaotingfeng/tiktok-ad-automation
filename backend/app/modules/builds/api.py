from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.builds import catalog, drafts, mutations, previews
from app.modules.builds.models import (
    BuildDraft,
    DraftPreparation,
    DraftPreparationRequest,
)
from app.modules.builds.preview_schemas import (
    FrozenGroup,
    FrozenUnit,
    PreviewAccepted,
    PreviewDramaPublic,
    PreviewInputPublic,
    PreviewRequest,
    PreviewSummary,
    PreviewUnit,
)
from app.modules.builds.schemas import (
    CreateDraftRequest,
    DraftDramaPublic,
    DraftGroupEditRequest,
    DraftInputPublic,
    DraftMaterialPublic,
    DraftPrepareAccepted,
    DraftPrepareRequest,
    DraftSaved,
    DraftSummary,
    PatchDraftRequest,
)
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["builds"])
Cursor = Annotated[str | None, Query(max_length=4096)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.post("/build-drafts", response_model=DraftSaved, status_code=201)
def create(
    tenant_id: UUID, body: CreateDraftRequest, session: SessionDep, user: CurrentUser
) -> DraftSaved:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    identity = drafts.create_draft(session, context=context, **body.model_dump())
    draft = drafts.get_draft(session, context=context, draft_id=identity)
    result = DraftSaved(draft_id=identity, revision=draft.revision)
    session.commit()
    return result


@router.get("/build-draft-requests/{request_id}", response_model=DraftSaved)
def saved_request(
    tenant_id: UUID, request_id: UUID, session: SessionDep, user: CurrentUser
) -> DraftSaved:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    row = session.exec(
        select(BuildDraft).where(
            BuildDraft.tenant_id == tenant_id, BuildDraft.request_id == request_id
        )
    ).one_or_none()
    if row is None:
        raise DomainError("draft_not_found", "草稿保存请求尚未找到")
    return DraftSaved(draft_id=row.id, revision=row.revision)


@router.get(
    "/build-preparation-requests/{request_id}", response_model=DraftPrepareAccepted
)
def saved_prepare_request(
    tenant_id: UUID, request_id: UUID, session: SessionDep, user: CurrentUser
) -> DraftPrepareAccepted:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    row = session.get(DraftPreparationRequest, (tenant_id, request_id))
    if row is None:
        raise DomainError("draft_not_found", "草稿准备请求尚未找到")
    prep = session.get(DraftPreparation, row.preparation_id)
    assert prep
    return DraftPrepareAccepted(task_id=prep.id, revision=prep.draft_revision)


@router.get("/build-drafts/{draft_id}", response_model=DraftSummary)
def summary(
    tenant_id: UUID, draft_id: UUID, session: SessionDep, user: CurrentUser
) -> DraftSummary:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.draft_summary(session, context=context, draft_id=draft_id)


@router.patch("/build-drafts/{draft_id}", response_model=DraftSaved)
def update(
    tenant_id: UUID,
    draft_id: UUID,
    body: PatchDraftRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DraftSaved:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    changes = body.model_dump(exclude_none=True)
    # 显式 null 表示清除草稿偏好；遗漏字段仍表示保持原选择。
    if "execution_connection_id" in body.model_fields_set:
        changes["execution_connection_id"] = body.execution_connection_id
    revision = mutations.update_draft(
        session,
        context=context,
        draft_id=draft_id,
        **changes,
    )
    session.commit()
    return DraftSaved(draft_id=draft_id, revision=revision)


@router.post(
    "/build-drafts/{draft_id}/prepare",
    response_model=DraftPrepareAccepted,
    status_code=202,
)
def prepare(
    tenant_id: UUID,
    draft_id: UUID,
    body: DraftPrepareRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DraftPrepareAccepted:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    task = drafts.prepare_draft(
        session, context=context, draft_id=draft_id, request_id=body.request_id
    )
    prep = session.get(DraftPreparation, task)
    assert prep
    result = DraftPrepareAccepted(task_id=task, revision=prep.draft_revision)
    session.commit()
    return result


@router.get("/build-drafts/{draft_id}/inputs", response_model=Page[DraftInputPublic])
def inputs(
    tenant_id: UUID,
    draft_id: UUID,
    kind: Literal["drama", "account"],
    session: SessionDep,
    user: CurrentUser,
    status: Annotated[str | None, Query(max_length=32)] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[DraftInputPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.inputs_page(
        session,
        context=context,
        draft_id=draft_id,
        kind=kind,
        status=status,
        cursor=cursor,
        limit=limit,
    )


@router.get("/build-drafts/{draft_id}/dramas", response_model=Page[DraftDramaPublic])
def dramas(
    tenant_id: UUID,
    draft_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[DraftDramaPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.dramas_page(
        session, context=context, draft_id=draft_id, cursor=cursor, limit=limit
    )


@router.get(
    "/build-drafts/{draft_id}/dramas/{drama_id}/materials",
    response_model=Page[DraftMaterialPublic],
)
def materials(
    tenant_id: UUID,
    draft_id: UUID,
    drama_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[DraftMaterialPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.materials_page(
        session,
        context=context,
        draft_id=draft_id,
        drama_id=drama_id,
        cursor=cursor,
        limit=limit,
    )


@router.patch(
    "/build-drafts/{draft_id}/dramas/{drama_id}/groups", response_model=DraftSaved
)
def edit_groups(
    tenant_id: UUID,
    draft_id: UUID,
    drama_id: UUID,
    body: DraftGroupEditRequest,
    session: SessionDep,
    user: CurrentUser,
) -> DraftSaved:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    revision = mutations.edit_material_groups(
        session,
        context=context,
        draft_id=draft_id,
        drama_id=drama_id,
        expected_revision=body.expected_revision,
        request_id=body.request_id,
        groups=body.groups,
    )
    session.commit()
    return DraftSaved(draft_id=draft_id, revision=revision)


@router.post(
    "/build-drafts/{draft_id}/previews", response_model=PreviewAccepted, status_code=202
)
def generate_preview(
    tenant_id: UUID,
    draft_id: UUID,
    body: PreviewRequest,
    session: SessionDep,
    user: CurrentUser,
) -> PreviewAccepted:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    identity = previews.generate_preview(
        session,
        context=context,
        draft_id=draft_id,
        expected_revision=body.expected_revision,
    )
    session.commit()
    return PreviewAccepted(preview_id=identity)


@router.get(
    "/build-drafts/{draft_id}/previews/{revision}", response_model=PreviewAccepted
)
def preview_request(
    tenant_id: UUID,
    draft_id: UUID,
    revision: int,
    session: SessionDep,
    user: CurrentUser,
) -> PreviewAccepted:
    from app.modules.builds.preview_models import BuildPreview

    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    identity = session.exec(
        select(BuildPreview.id).where(
            BuildPreview.tenant_id == tenant_id,
            BuildPreview.draft_id == draft_id,
            BuildPreview.draft_revision == revision,
        )
    ).one_or_none()
    if identity is None:
        raise DomainError("preview_not_found", "预览请求尚未找到")
    return PreviewAccepted(preview_id=identity)


@router.get("/build-previews/{preview_id}", response_model=PreviewSummary)
def preview_summary(
    tenant_id: UUID, preview_id: UUID, session: SessionDep, user: CurrentUser
) -> PreviewSummary:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.get_preview_summary(session, context=context, preview_id=preview_id)


@router.get("/build-previews/{preview_id}/units", response_model=Page[PreviewUnit])
def preview_units(
    tenant_id: UUID,
    preview_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
    readiness: Literal["READY", "PREPARING", "BLOCKED"] | None = None,
    drama_id: UUID | None = None,
) -> Page[PreviewUnit]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.get_preview_units(
        session,
        context=context,
        preview_id=preview_id,
        cursor=cursor,
        limit=limit,
        readiness=readiness,
        drama_id=drama_id,
    )


@router.get(
    "/build-previews/{preview_id}/inputs", response_model=Page[PreviewInputPublic]
)
def preview_inputs(
    tenant_id: UUID,
    preview_id: UUID,
    kind: Literal["drama", "account"],
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
    issues_only: bool = False,
    status: Annotated[str | None, Query(max_length=32)] = None,
) -> Page[PreviewInputPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.get_preview_inputs(
        session,
        context=context,
        preview_id=preview_id,
        kind=kind,
        cursor=cursor,
        limit=limit,
        issues_only=issues_only,
        status=status,
    )


@router.get("/build-units/{unit_id}", response_model=FrozenUnit)
def frozen_unit(
    tenant_id: UUID, unit_id: UUID, session: SessionDep, user: CurrentUser
) -> FrozenUnit:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.load_frozen_unit(session, context=context, unit_id=unit_id)


@router.get("/build-units/{unit_id}/groups", response_model=Page[FrozenGroup])
def frozen_groups(
    tenant_id: UUID,
    unit_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 20,
) -> Page[FrozenGroup]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return previews.get_frozen_groups(
        session, context=context, unit_id=unit_id, cursor=cursor, limit=limit
    )


@router.get("/build-mutation-requests/{request_id}", response_model=DraftSaved)
def saved_mutation(
    tenant_id: UUID, request_id: UUID, session: SessionDep, user: CurrentUser
) -> DraftSaved:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return mutations.saved_mutation(session, context=context, request_id=request_id)


@router.get(
    "/build-previews/{preview_id}/dramas", response_model=Page[PreviewDramaPublic]
)
def preview_dramas(
    tenant_id: UUID,
    preview_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[PreviewDramaPublic]:
    from app.modules.builds.preview_catalog import get_preview_dramas

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return get_preview_dramas(
        session, context=context, preview_id=preview_id, cursor=cursor, limit=limit
    )
