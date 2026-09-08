"""Read-only task workspace endpoints; registration is owned by the main router."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.core.pagination import Page
from app.modules.builds import submission_catalog as catalog
from app.modules.builds.execution_schemas import (
    SubmissionAdPublic,
    SubmissionEventPublic,
    SubmissionGroupPublic,
    SubmissionListItem,
)
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["builds"])
Cursor = Annotated[str | None, Query(max_length=4096)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.get(
    "/submissions",
    response_model=Page[SubmissionListItem],
    operation_id="builds-list_submissions",
)
def list_submissions(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    q: Annotated[str | None, Query(max_length=255)] = None,
    status_group: catalog.StatusGroup = "all",
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    provider_connection_id: UUID | None = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[SubmissionListItem]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.list_submissions(
        session,
        context=context,
        bc_id=bc_id,
        q=q,
        status_group=status_group,
        created_from=created_from,
        created_to=created_to,
        provider_connection_id=provider_connection_id,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/submissions/{submission_id}/units/{unit_id}/groups",
    response_model=Page[SubmissionGroupPublic],
    operation_id="builds-get_submission_groups",
)
def get_submission_groups(
    tenant_id: UUID,
    submission_id: UUID,
    unit_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[SubmissionGroupPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.get_submission_groups(
        session,
        context=context,
        submission_id=submission_id,
        unit_id=unit_id,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/submissions/{submission_id}/units/{unit_id}/groups/{group_id}/ads",
    response_model=Page[SubmissionAdPublic],
    operation_id="builds-get_submission_ads",
)
def get_submission_ads(
    tenant_id: UUID,
    submission_id: UUID,
    unit_id: UUID,
    group_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[SubmissionAdPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.get_submission_ads(
        session,
        context=context,
        submission_id=submission_id,
        unit_id=unit_id,
        group_id=group_id,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/submissions/{submission_id}/events",
    response_model=Page[SubmissionEventPublic],
    operation_id="builds-get_submission_events",
)
def get_submission_events(
    tenant_id: UUID,
    submission_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    step_id: UUID | None = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[SubmissionEventPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return catalog.get_submission_events(
        session,
        context=context,
        submission_id=submission_id,
        step_id=step_id,
        cursor=cursor,
        limit=limit,
    )
