"""Submission boundaries; all GET handlers remain local and read-only."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.core.pagination import Page
from app.modules.builds import submissions
from app.modules.builds.execution_schemas import (
    StepPublic,
    SubmissionReceipt,
    SubmissionUnitPublic,
    SubmissionView,
    SubmitRequest,
)
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["builds"])
Cursor = Annotated[str | None, Query(max_length=4096)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.post(
    "/build-previews/{preview_id}/submit",
    response_model=SubmissionReceipt,
    status_code=202,
    operation_id="builds-submit_preview",
)
def submit_preview(
    tenant_id: UUID,
    preview_id: UUID,
    body: SubmitRequest,
    session: SessionDep,
    user: CurrentUser,
) -> SubmissionReceipt:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    receipt = submissions.submit_preview(
        session, context=context, preview_id=preview_id, request_id=body.request_id
    )
    session.commit()
    return receipt


@router.get(
    "/submissions/{submission_id}",
    response_model=SubmissionView,
    operation_id="builds-get_submission",
)
def get_submission(
    tenant_id: UUID, submission_id: UUID, session: SessionDep, user: CurrentUser
) -> SubmissionView:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return submissions.get_submission(
        session, context=context, submission_id=submission_id
    )


@router.get(
    "/submissions/{submission_id}/units",
    response_model=Page[SubmissionUnitPublic],
    operation_id="builds-get_submission_units",
)
def get_submission_units(
    tenant_id: UUID,
    submission_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
    advertiser_id: str | None = None,
    drama_id: UUID | None = None,
) -> Page[SubmissionUnitPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return submissions.get_submission_units(
        session,
        context=context,
        submission_id=submission_id,
        cursor=cursor,
        limit=limit,
        advertiser_id=advertiser_id,
        drama_id=drama_id,
    )


@router.get(
    "/submissions/{submission_id}/excluded",
    response_model=Page[SubmissionUnitPublic],
    operation_id="builds-get_submission_excluded",
)
def get_submission_excluded(
    tenant_id: UUID,
    submission_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
    advertiser_id: str | None = None,
    drama_id: UUID | None = None,
) -> Page[SubmissionUnitPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return submissions.get_submission_units(
        session,
        context=context,
        submission_id=submission_id,
        cursor=cursor,
        limit=limit,
        advertiser_id=advertiser_id,
        drama_id=drama_id,
        excluded_only=True,
    )


@router.get(
    "/submissions/{submission_id}/steps",
    response_model=Page[StepPublic],
    operation_id="builds-get_submission_steps",
)
def get_submission_steps(
    tenant_id: UUID,
    submission_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
    advertiser_id: str | None = None,
    drama_id: UUID | None = None,
    kind: str | None = None,
    result: str | None = None,
) -> Page[StepPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return submissions.get_submission_steps(
        session,
        context=context,
        submission_id=submission_id,
        cursor=cursor,
        limit=limit,
        advertiser_id=advertiser_id,
        drama_id=drama_id,
        kind=kind,
        result=result,
    )
