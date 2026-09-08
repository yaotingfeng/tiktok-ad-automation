from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.core.pagination import Page
from app.modules.strategies import service
from app.modules.strategies.schemas import (
    AppendVersionRequest,
    CopyPoolPublic,
    CreateStrategyRequest,
    StrategyConfig,
    StrategyPublic,
    StrategyStateRequest,
    ValidationResult,
    VersionPublic,
)
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["strategies"])
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=4096)]


@router.get("/strategies", response_model=Page[StrategyPublic])
def get_strategies(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    query: Annotated[str, Query(max_length=255)] = "",
    active: bool | None = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[StrategyPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.list_strategies(
        session, context=context, query=query, active=active, cursor=cursor, limit=limit
    )


@router.post("/strategies/validate", response_model=ValidationResult)
def validate(
    tenant_id: UUID, body: StrategyConfig, session: SessionDep, user: CurrentUser
) -> ValidationResult:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    errors = service.validate_strategy(session, context=context, config=body)
    return ValidationResult(valid=not errors, errors=errors)


@router.post("/strategies", response_model=StrategyPublic, status_code=201)
def create(
    tenant_id: UUID, body: CreateStrategyRequest, session: SessionDep, user: CurrentUser
) -> StrategyPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="strategy_write"
    )
    identity = service.create_strategy(
        session,
        context=context,
        name=body.name,
        config=body.config,
        request_id=body.request_id,
    )
    result = service.get_strategy(session, context=context, strategy_id=identity)
    session.commit()
    return result


@router.get("/strategies/{strategy_id}", response_model=StrategyPublic)
def get_one(
    tenant_id: UUID, strategy_id: UUID, session: SessionDep, user: CurrentUser
) -> StrategyPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.get_strategy(session, context=context, strategy_id=strategy_id)


@router.patch("/strategies/{strategy_id}", response_model=StrategyPublic)
def set_state(
    tenant_id: UUID,
    strategy_id: UUID,
    body: StrategyStateRequest,
    session: SessionDep,
    user: CurrentUser,
) -> StrategyPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="strategy_write"
    )
    result = service.set_active(
        session, context=context, strategy_id=strategy_id, active=body.active
    )
    session.commit()
    return result


@router.post(
    "/strategies/{strategy_id}/versions", response_model=VersionPublic, status_code=201
)
def append(
    tenant_id: UUID,
    strategy_id: UUID,
    body: AppendVersionRequest,
    session: SessionDep,
    user: CurrentUser,
) -> VersionPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="strategy_write"
    )
    identity = service.append_version(
        session,
        context=context,
        strategy_id=strategy_id,
        config=body.config,
        request_id=body.request_id,
        expected_version=body.expected_version,
    )
    result = service.get_version_record(session, context=context, version_id=identity)
    session.commit()
    return result


@router.get("/strategies/{strategy_id}/versions", response_model=Page[VersionPublic])
def versions(
    tenant_id: UUID,
    strategy_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[VersionPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.list_versions(
        session, context=context, strategy_id=strategy_id, cursor=cursor, limit=limit
    )


@router.get("/strategy-versions/{version_id}", response_model=VersionPublic)
def version(
    tenant_id: UUID, version_id: UUID, session: SessionDep, user: CurrentUser
) -> VersionPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.get_version_record(session, context=context, version_id=version_id)


@router.get("/strategy-save-requests/{request_id}", response_model=VersionPublic)
def saved_request(
    tenant_id: UUID, request_id: UUID, session: SessionDep, user: CurrentUser
) -> VersionPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.get_saved_request(session, context=context, request_id=request_id)


@router.get("/copy-pools/{version_id}", response_model=CopyPoolPublic)
def copy_pool(
    tenant_id: UUID, version_id: UUID, session: SessionDep, user: CurrentUser
) -> CopyPoolPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.get_copy_pool(session, context=context, version_id=version_id)
