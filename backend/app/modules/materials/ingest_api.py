"""Tenant-scoped metadata APIs; browser bytes go directly to private R2."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep
from app.core.db import engine
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.materials import ingest_service as service
from app.modules.materials.ingest_models import IngestChunk, IngestSession
from app.modules.materials.ingest_schemas import (
    IngestChunkCreate,
    IngestChunkResult,
    IngestFilePublic,
    IngestIdentity,
    IngestPartsPage,
    IngestPartUrls,
    IngestPartUrlsCreate,
    IngestSealResult,
    IngestSessionCreate,
    IngestSummary,
)
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}/materials", tags=["material-ingest"])
Cursor = Annotated[str | None, Query(max_length=8192)]
Limit = Annotated[int, Query(ge=1, le=100)]
BCID = Annotated[str, Query(min_length=1, max_length=128)]


@router.post(
    "/ingest-sessions",
    response_model=IngestSummary,
    status_code=201,
    operation_id="create_ingest_session",
)
def create_ingest_session(
    tenant_id: UUID, body: IngestSessionCreate, session: SessionDep, user: CurrentUser
) -> IngestSummary:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    result = service.create_session(session, context=context, body=body)
    session.commit()
    return result


@router.get(
    "/ingest-requests/{request_id}",
    response_model=IngestSummary,
    operation_id="read_ingest_request",
)
def read_ingest_request(
    tenant_id: UUID,
    request_id: UUID,
    bc_id: BCID,
    session: SessionDep,
    user: CurrentUser,
) -> IngestSummary:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    service.require_bc(session, context=context, bc_id=bc_id, action="read")
    row = session.exec(
        select(IngestSession).where(
            IngestSession.tenant_id == tenant_id,
            IngestSession.bc_id == bc_id,
            IngestSession.request_id == request_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError("upload_batch_not_found", "导入会话不存在或不可见")
    return service.summary(row)


@router.get(
    "/ingest-sessions",
    response_model=Page[IngestSummary],
    operation_id="list_ingest_sessions",
)
def list_ingest_sessions(
    tenant_id: UUID,
    bc_id: BCID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[IngestSummary]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.sessions_page(
        session, context=context, bc_id=bc_id, cursor=cursor, limit=limit
    )


@router.get(
    "/ingest-sessions/{session_id}",
    response_model=IngestSummary,
    operation_id="read_ingest_summary",
)
def read_ingest_summary(
    tenant_id: UUID, session_id: UUID, session: SessionDep, user: CurrentUser
) -> IngestSummary:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.summary(
        service.get_session(session, context=context, session_id=session_id)
    )


@router.post(
    "/ingest-sessions/{session_id}/chunks",
    response_model=IngestChunkResult,
    status_code=201,
    operation_id="create_ingest_chunk",
)
def create_ingest_chunk(
    tenant_id: UUID,
    session_id: UUID,
    body: IngestChunkCreate,
    session: SessionDep,
    user: CurrentUser,
) -> IngestChunkResult:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    result = service.register_chunk(
        session, context=context, session_id=session_id, body=body
    )
    session.commit()
    return result


@router.get(
    "/ingest-sessions/{session_id}/chunks/{request_id}",
    response_model=IngestChunkResult,
    operation_id="read_ingest_chunk",
)
def read_ingest_chunk(
    tenant_id: UUID,
    session_id: UUID,
    request_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> IngestChunkResult:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    service.get_session(session, context=context, session_id=session_id)
    receipt = session.exec(
        select(IngestChunk).where(
            IngestChunk.tenant_id == tenant_id,
            IngestChunk.session_id == session_id,
            IngestChunk.request_id == request_id,
        )
    ).one_or_none()
    if receipt is None:
        raise DomainError("upload_batch_not_found", "子批次不存在或不可见")
    return service.chunk_result(session, context=context, receipt=receipt)


@router.post(
    "/ingest-sessions/{session_id}/seal",
    response_model=IngestSealResult,
    operation_id="seal_ingest_session",
)
def seal_ingest_session(
    tenant_id: UUID, session_id: UUID, session: SessionDep, user: CurrentUser
) -> IngestSealResult:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    result = service.seal_session(session, context=context, session_id=session_id)
    session.commit()
    return result


@router.get(
    "/ingest-sessions/{session_id}/files",
    response_model=Page[IngestFilePublic],
    operation_id="list_ingest_files",
)
def list_ingest_files(
    tenant_id: UUID,
    session_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 100,
    status: Annotated[str | None, Query(max_length=32)] = None,
) -> Page[IngestFilePublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.files_page(
        session,
        context=context,
        session_id=session_id,
        cursor=cursor,
        limit=limit,
        status=status,
    )


@router.get(
    "/ingest-sessions/{session_id}/files/{material_id}",
    response_model=IngestFilePublic,
    operation_id="read_ingest_file",
)
def read_ingest_file(
    tenant_id: UUID,
    session_id: UUID,
    material_id: UUID,
    session: SessionDep,
    user: CurrentUser,
) -> IngestFilePublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return service.read_file(
        session, context=context, session_id=session_id, material_id=material_id
    )


@router.post(
    "/ingest-sessions/{session_id}/files/{material_id}/resume",
    response_model=IngestFilePublic,
    operation_id="resume_ingest_file",
)
def resume_ingest_file(
    tenant_id: UUID,
    session_id: UUID,
    material_id: UUID,
    body: IngestIdentity,
    session: SessionDep,
    user: CurrentUser,
) -> IngestFilePublic:
    from .ingest_transport import resume_file

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    session.rollback()
    return resume_file(
        database_engine=engine,
        context=context,
        session_id=session_id,
        material_id=material_id,
        identity=body,
    )


@router.post(
    "/ingest-sessions/{session_id}/files/{material_id}/part-urls",
    response_model=IngestPartUrls,
    operation_id="sign_ingest_parts",
)
def sign_ingest_parts(
    tenant_id: UUID,
    session_id: UUID,
    material_id: UUID,
    body: IngestPartUrlsCreate,
    response: Response,
    session: SessionDep,
    user: CurrentUser,
) -> IngestPartUrls:
    from .ingest_transport import sign_parts

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    session.rollback()
    response.headers["Cache-Control"] = "no-store"
    return sign_parts(
        database_engine=engine,
        context=context,
        session_id=session_id,
        material_id=material_id,
        identity=body,
    )


@router.get(
    "/ingest-sessions/{session_id}/files/{material_id}/parts",
    response_model=IngestPartsPage,
    operation_id="list_ingest_parts",
)
def list_ingest_parts(
    tenant_id: UUID,
    session_id: UUID,
    material_id: UUID,
    generation: Annotated[int, Query(ge=1)],
    upload_id: Annotated[str, Query(min_length=1, max_length=512)],
    operation_revision: Annotated[int, Query(ge=0)],
    response: Response,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 100,
) -> IngestPartsPage:
    from .ingest_transport import list_parts

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    session.rollback()
    response.headers["Cache-Control"] = "no-store"
    return list_parts(
        database_engine=engine,
        context=context,
        session_id=session_id,
        material_id=material_id,
        identity=IngestIdentity(
            generation=generation,
            upload_id=upload_id,
            operation_revision=operation_revision,
        ),
        cursor=cursor,
        limit=limit,
    )


@router.post(
    "/ingest-sessions/{session_id}/files/{material_id}/complete",
    response_model=IngestFilePublic,
    operation_id="complete_ingest_file",
)
def complete_ingest_file(
    tenant_id: UUID,
    session_id: UUID,
    material_id: UUID,
    body: IngestIdentity,
    session: SessionDep,
    user: CurrentUser,
) -> IngestFilePublic:
    from .ingest_transport import complete_file

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    session.rollback()
    return complete_file(
        database_engine=engine,
        context=context,
        session_id=session_id,
        material_id=material_id,
        identity=body,
    )


@router.post(
    "/ingest-sessions/{session_id}/files/{material_id}/cancel",
    response_model=IngestFilePublic,
    operation_id="cancel_ingest_file",
)
def cancel_ingest_file(
    tenant_id: UUID,
    session_id: UUID,
    material_id: UUID,
    body: IngestIdentity,
    session: SessionDep,
    user: CurrentUser,
) -> IngestFilePublic:
    from .ingest_transport import cancel_file

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    session.rollback()
    return cancel_file(
        database_engine=engine,
        context=context,
        session_id=session_id,
        material_id=material_id,
        identity=body,
    )


@router.post(
    "/ingest-sessions/{session_id}/files/{material_id}/new-generation",
    response_model=IngestFilePublic,
    operation_id="create_ingest_generation",
)
def create_ingest_generation(
    tenant_id: UUID,
    session_id: UUID,
    material_id: UUID,
    body: IngestIdentity,
    session: SessionDep,
    user: CurrentUser,
) -> IngestFilePublic:
    from .ingest_transport import new_generation

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="upload"
    )
    session.rollback()
    return new_generation(
        database_engine=engine,
        context=context,
        session_id=session_id,
        material_id=material_id,
        identity=body,
    )
