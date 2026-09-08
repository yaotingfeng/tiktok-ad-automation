"""Explicit capability refresh command; status reads never enqueue work."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Response

from app.api.deps import CurrentUser, SessionDep
from app.modules.accounts.capabilities import (
    get_capability_job,
    start_capability_refresh,
)
from app.modules.accounts.capability_schemas import (
    CapabilityJobPublic,
    CapabilityRefreshRequest,
    public_job,
)
from app.modules.tenants.permissions import require_tenant

router = APIRouter(
    prefix="/tenants/{tenant_id}/bcs/{bc_id}/capability-refresh", tags=["accounts"]
)
BCID = Annotated[str, Path(min_length=1, max_length=128)]


@router.post("", response_model=CapabilityJobPublic)
def refresh_capabilities(
    tenant_id: UUID,
    bc_id: BCID,
    body: CapabilityRefreshRequest,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
) -> CapabilityJobPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="build"
    )
    job_id = start_capability_refresh(
        session,
        context=context,
        bc_id=bc_id,
        connection_id=body.connection_id,
        request_id=body.request_id,
    )
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    return public_job(
        get_capability_job(session, context=context, bc_id=bc_id, job_id=job_id)
    )


@router.get("/{job_id}", response_model=CapabilityJobPublic)
def capability_status(
    tenant_id: UUID,
    bc_id: BCID,
    job_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
) -> CapabilityJobPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    response.headers["Cache-Control"] = "no-store"
    return public_job(
        get_capability_job(session, context=context, bc_id=bc_id, job_id=job_id)
    )
