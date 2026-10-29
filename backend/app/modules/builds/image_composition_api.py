"""授权范围由 URL + 登录用户解析；请求体不接受 tenant/BC/route。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import Field

from app.api.deps import CurrentUser, SessionDep
from app.core.pagination import Page
from app.modules.builds import image_composition_service as service
from app.modules.builds.image_composition_commands import CreativeEditCommand
from app.modules.builds.image_composition_schemas import (
    ImageRule,
    ImageRulePatch,
    ImmutableModel,
)
from app.modules.tenants.permissions import require_tenant

from . import image_public as public

router = APIRouter(prefix="/build-drafts/{draft_id}/image-composition")
Cursor = Annotated[str | None, Query(max_length=4096)]
Limit = Annotated[int, Query(ge=1, le=100)]


class Change(ImmutableModel):
    request_id: UUID
    expected_revision: int = Field(ge=1, strict=True)


class Preview(Change):
    rule: ImageRule
    drama_patches: dict[UUID, ImageRulePatch] = Field(default_factory=dict)
    mode: Literal["PRESERVE_MANUAL", "REPLACE_ALL"] = "PRESERVE_MANUAL"


class Apply(Change):
    plan_id: UUID


class Adopt(Change):
    snapshot_id: UUID


class Allocate(Change):
    material_id: UUID
    drama_ids: tuple[UUID, ...] = Field(min_length=1, max_length=1000)


class Edit(Change):
    command: CreativeEditCommand


class Exclude(Change):
    drama_id: UUID
    material_ids: tuple[UUID, ...] = Field(min_length=1, max_length=1000)


def _scope(session, user, tenant_id, draft_id, *, write=False, cancel=False):
    context = require_tenant(
        session,
        actor_id=user.id,
        tenant_id=tenant_id,
        action="build" if write else "read",
    )
    return service.resolve_image_scope(
        session,
        context=context,
        draft_id=draft_id,
        allow_frozen_read=not write or cancel,
    )


@router.get(
    "", response_model=public.CompositionStatePublic, response_model_exclude_unset=True
)
def composition_state(
    tenant_id: UUID, draft_id: UUID, session: SessionDep, user: CurrentUser
):
    return service.composition_state(
        session, _scope(session, user, tenant_id, draft_id)
    )


@router.post("/candidates/refresh", response_model=public.CandidateProgress)
def refresh(
    tenant_id: UUID,
    draft_id: UUID,
    body: Change,
    session: SessionDep,
    user: CurrentUser,
):
    result = service.refresh_candidates(
        session,
        _scope(session, user, tenant_id, draft_id, write=True),
        **body.model_dump(),
    )
    session.commit()
    return result


@router.post("/candidates/adopt", response_model=public.CandidateAdopted)
def adopt(
    tenant_id: UUID, draft_id: UUID, body: Adopt, session: SessionDep, user: CurrentUser
):
    result = service.adopt_candidates(
        session,
        _scope(session, user, tenant_id, draft_id, write=True),
        **body.model_dump(),
    )
    session.commit()
    return result


@router.post("/candidates/allocate", response_model=public.ImageRevisionPublic)
def allocate(
    tenant_id: UUID,
    draft_id: UUID,
    body: Allocate,
    session: SessionDep,
    user: CurrentUser,
):
    result = service.resolve_candidate_conflict(
        session,
        _scope(session, user, tenant_id, draft_id, write=True),
        **body.model_dump(),
    )
    session.commit()
    return result


@router.post("/candidates/exclude", response_model=public.ImageRevisionPublic)
def exclude(
    tenant_id: UUID,
    draft_id: UUID,
    body: Exclude,
    session: SessionDep,
    user: CurrentUser,
):
    result = service.exclude_candidates(
        session,
        _scope(session, user, tenant_id, draft_id, write=True),
        **body.model_dump(),
    )
    session.commit()
    return result


@router.get("/candidates", response_model=Page[public.ImageCandidatePublic])
def candidates(
    tenant_id: UUID,
    draft_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    snapshot_id: UUID | None = None,
    cursor: Cursor = None,
    limit: Limit = 50,
    drama_id: UUID | None = None,
):
    return service.list_candidates(
        session,
        _scope(session, user, tenant_id, draft_id),
        snapshot_id,
        cursor=cursor,
        limit=limit,
        drama_id=drama_id,
    )


@router.post("/plans", response_model=public.CompositionPlanPublic)
def preview(
    tenant_id: UUID,
    draft_id: UUID,
    body: Preview,
    session: SessionDep,
    user: CurrentUser,
):
    result = service.preview_composition(
        session,
        _scope(session, user, tenant_id, draft_id, write=True),
        body.request_id,
        body.expected_revision,
        body.rule,
        body.drama_patches,
        body.mode,
    )
    session.commit()
    return result


@router.get("/plans/{plan_id}", response_model=public.CompositionPlanPublic)
def plan(
    tenant_id: UUID,
    draft_id: UUID,
    plan_id: UUID,
    session: SessionDep,
    user: CurrentUser,
):
    scope = _scope(session, user, tenant_id, draft_id)
    return service.get_composition_plan(session, scope, plan_id)


@router.get("/plans/{plan_id}/units", response_model=Page[public.ImageUnitPublic])
def plan_units(
    tenant_id: UUID,
    draft_id: UUID,
    plan_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
):
    return service.list_plan_units(
        session,
        _scope(session, user, tenant_id, draft_id),
        plan_id,
        cursor=cursor,
        limit=limit,
    )


@router.post("/apply", response_model=public.CompositionRunPublic)
def apply(
    tenant_id: UUID, draft_id: UUID, body: Apply, session: SessionDep, user: CurrentUser
):
    result = service.apply_composition(
        session,
        _scope(session, user, tenant_id, draft_id, write=True),
        **body.model_dump(),
    )
    session.commit()
    return result


@router.get("/runs/{run_id}", response_model=public.CompositionRunPublic)
def progress(
    tenant_id: UUID,
    draft_id: UUID,
    run_id: UUID,
    session: SessionDep,
    user: CurrentUser,
):
    return service.composition_progress(
        session, _scope(session, user, tenant_id, draft_id), run_id
    )


@router.post("/runs/{run_id}/cancel", response_model=public.CompositionRunPublic)
def cancel(
    tenant_id: UUID,
    draft_id: UUID,
    run_id: UUID,
    body: Change,
    session: SessionDep,
    user: CurrentUser,
):
    result = service.cancel_composition(
        session,
        _scope(session, user, tenant_id, draft_id, write=True, cancel=True),
        run_id,
        **body.model_dump(),
    )
    session.commit()
    return result


@router.get("/units", response_model=Page[public.ImageUnitPublic])
def units(
    tenant_id: UUID,
    draft_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
    drama_id: UUID | None = None,
):
    return service.list_creative_units(
        session,
        _scope(session, user, tenant_id, draft_id),
        cursor=cursor,
        limit=limit,
        drama_id=drama_id,
    )


@router.post("/edits", response_model=public.CreativeEditPublic)
def edit(
    tenant_id: UUID, draft_id: UUID, body: Edit, session: SessionDep, user: CurrentUser
):
    result = service.edit_creative_units(
        session,
        _scope(session, user, tenant_id, draft_id, write=True),
        body.request_id,
        body.expected_revision,
        body.command,
    )
    session.commit()
    return result


@router.post("/candidates/add", response_model=public.CandidateAdded)
def add_candidates(
    tenant_id: UUID,
    draft_id: UUID,
    body: Exclude,
    session: SessionDep,
    user: CurrentUser,
):
    result = service.add_candidate_images(
        session,
        _scope(session, user, tenant_id, draft_id, write=True),
        **body.model_dump(),
    )
    session.commit()
    return result


@router.get("/plans/{plan_id}/differences", response_model=Page[public.ImageDifference])
def differences(
    tenant_id: UUID,
    draft_id: UUID,
    plan_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    kind: Literal[
        "conflicts",
        "excluded_images",
        "warnings",
        "removed_ids",
        "retained_ids",
        "protection_losses",
    ] = "conflicts",
    cursor: Cursor = None,
    limit: Limit = 50,
):
    return service.list_plan_differences(
        session,
        _scope(session, user, tenant_id, draft_id),
        plan_id,
        kind=kind,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/candidates/snapshots/{snapshot_id}", response_model=public.CandidateProgress
)
def candidate_progress(
    tenant_id: UUID,
    draft_id: UUID,
    snapshot_id: UUID,
    session: SessionDep,
    user: CurrentUser,
):
    from .image_candidate_service import candidate_snapshot_progress

    return candidate_snapshot_progress(
        session, _scope(session, user, tenant_id, draft_id), snapshot_id
    )


@router.post(
    "/candidates/snapshots/{snapshot_id}/cancel",
    response_model=public.CandidateProgress,
)
def cancel_candidates(
    tenant_id: UUID,
    draft_id: UUID,
    snapshot_id: UUID,
    body: Change,
    session: SessionDep,
    user: CurrentUser,
):
    from .image_candidate_service import cancel_candidate_snapshot

    result = cancel_candidate_snapshot(
        session,
        _scope(session, user, tenant_id, draft_id, write=True, cancel=True),
        snapshot_id,
        **body.model_dump(),
    )
    session.commit()
    return result


@router.get("/dramas", response_model=Page[public.ImageDramaSummary])
def image_dramas(
    tenant_id: UUID,
    draft_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    issue: Literal["all", "music", "unused", "protected"] = "all",
    cursor: Cursor = None,
    limit: Limit = 50,
):
    from .image_composition_queries import drama_summaries

    return drama_summaries(
        session,
        _scope(session, user, tenant_id, draft_id),
        issue=issue,
        cursor=cursor,
        limit=limit,
    )
