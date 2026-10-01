"""HTTP boundary for scoped management previews and task actions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.core.pagination import Page
from app.modules.ad_management.actions import (
    cancel_task,
    get_task,
    list_tasks,
    prepare_restore,
    reconcile,
    retry_task,
)
from app.modules.ad_management.previews import prepare_preview
from app.modules.ad_management.schemas import (
    ManagementPreviewPublic,
    ManagementPreviewRequest,
    ManagementRetryRequest,
    ManagementTaskPublic,
    ManagementTaskRequest,
)
from app.modules.ad_management.submissions import submit_management_task
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["ad_management"])
Cursor = Annotated[str | None, Query(max_length=128)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.post(
    "/ad-management-previews",
    response_model=ManagementPreviewPublic,
    status_code=201,
    operation_id="ad_management-prepare_preview",
)
def prepare_preview_route(
    tenant_id: UUID,
    body: ManagementPreviewRequest,
    session: SessionDep,
    user: CurrentUser,
) -> ManagementPreviewPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="ads_manage"
    )
    result = prepare_preview(
        session,
        context=context,
        bc_id=body.bc_id,
        selection_id=body.selection_id,
        mutation=body.mutation,
    )
    session.commit()
    return result


@router.post(
    "/ad-management-tasks",
    response_model=ManagementTaskPublic,
    status_code=202,
    operation_id="ad_management-submit_task",
)
def submit_task_route(
    tenant_id: UUID,
    body: ManagementTaskRequest,
    session: SessionDep,
    user: CurrentUser,
) -> ManagementTaskPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="ads_manage"
    )
    result = submit_management_task(
        session,
        context,
        body.preview_id,
        body.preview_digest,
        body.idempotency_key,
    )
    session.commit()
    return result


@router.get(
    "/ad-management-tasks",
    response_model=Page[ManagementTaskPublic],
    operation_id="ad_management-list_tasks",
)
def list_tasks_route(
    tenant_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[ManagementTaskPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return list_tasks(session, context, bc_id, cursor=cursor, limit=limit)


@router.get(
    "/ad-management-tasks/{task_id}",
    response_model=ManagementTaskPublic,
    operation_id="ad_management-get_task",
)
def get_task_route(
    tenant_id: UUID,
    task_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> ManagementTaskPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    from sqlmodel import select

    from app.modules.ad_management.models import ManagementTask

    row = session.exec(
        select(ManagementTask).where(
            ManagementTask.id == task_id,
            ManagementTask.tenant_id == tenant_id,
        )
    ).one_or_none()
    if row is None:
        from app.core.errors import DomainError

        raise DomainError("management_task_not_found", "管理任务不存在")
    return get_task(session, context, row.bc_id, task_id)


@router.post(
    "/ad-management-tasks/{task_id}/cancel",
    response_model=ManagementTaskPublic,
    operation_id="ad_management-cancel_task",
)
def cancel_task_route(
    tenant_id: UUID,
    task_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> ManagementTaskPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="ads_manage"
    )
    result = cancel_task(session, context, task_id)
    session.commit()
    return result


@router.post(
    "/ad-management-tasks/{task_id}/retry",
    response_model=ManagementTaskPublic,
    status_code=202,
    operation_id="ad_management-retry_task",
)
def retry_task_route(
    tenant_id: UUID,
    task_id: UUID,
    body: ManagementRetryRequest,
    session: SessionDep,
    user: CurrentUser,
) -> ManagementTaskPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="ads_manage"
    )
    result = retry_task(session, context, task_id, body.idempotency_key)
    session.commit()
    return result


@router.post(
    "/ad-management-tasks/{task_id}/restore",
    response_model=ManagementPreviewPublic,
    status_code=201,
    operation_id="ad_management-prepare_restore",
)
def restore_task_route(
    tenant_id: UUID,
    task_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> ManagementPreviewPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="ads_manage"
    )
    result = prepare_restore(session, context, task_id)
    session.commit()
    return result


@router.post(
    "/ad-management-tasks/{task_id}/reconcile",
    response_model=ManagementTaskPublic,
    operation_id="ad_management-reconcile_task",
)
def reconcile_task_route(
    tenant_id: UUID,
    task_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> ManagementTaskPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    result = reconcile(session, context, task_id)
    session.commit()
    return result


__all__ = ["router"]
