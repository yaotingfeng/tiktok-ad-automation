"""Recover exact PATCH results without inferring success from a later revision."""

import json
from collections.abc import Callable
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.builds import drafts
from app.modules.builds.mutation_models import DraftMutationRequest
from app.modules.builds.schemas import DraftSaved
from app.modules.tenants.permissions import require_tenant


def _apply(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    request_id: UUID | None,
    kind: str,
    intent: dict[str, Any],
    operation: Callable[[], int],
) -> int:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    if request_id is None:
        return operation()  # Backwards compatibility; new clients always supply a key.
    digest = sha256(
        json.dumps(
            {"draft_id": str(draft_id), "kind": kind, "intent": intent},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    lock = int.from_bytes(
        sha256(f"draft-mutation:{context.tenant_id}:{request_id}".encode()).digest()[
            :8
        ],
        signed=True,
    )
    cast(SQLAlchemySession, session).execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock}
    )
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    previous = session.get(
        DraftMutationRequest, (context.tenant_id, request_id), populate_existing=True
    )
    if previous:
        if previous.request_digest != digest:
            raise DomainError("idempotency_conflict", "该修改请求已用于不同内容")
        return previous.applied_revision
    revision = operation()
    session.add(
        DraftMutationRequest(
            tenant_id=context.tenant_id,
            request_id=request_id,
            draft_id=draft_id,
            actor_id=context.actor_id,
            kind=kind,
            request_digest=digest,
            applied_revision=revision,
        )
    )
    session.flush()
    return revision


def update_draft(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    request_id: UUID | None = None,
    **intent: Any,
) -> int:
    return _apply(
        session,
        context=context,
        draft_id=draft_id,
        request_id=request_id,
        kind="update",
        intent=intent,
        operation=lambda: drafts.update_draft(
            session, context=context, draft_id=draft_id, **intent
        ),
    )


def edit_material_groups(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    request_id: UUID | None = None,
    **intent: Any,
) -> int:
    return _apply(
        session,
        context=context,
        draft_id=draft_id,
        request_id=request_id,
        kind="groups",
        intent=intent,
        operation=lambda: drafts.edit_material_groups(
            session, context=context, draft_id=draft_id, **intent
        ),
    )


def saved_mutation(
    session: Session, *, context: TenantContext, request_id: UUID
) -> DraftSaved:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    row = session.get(
        DraftMutationRequest, (context.tenant_id, request_id), populate_existing=True
    )
    if row is None:
        raise DomainError("draft_not_found", "修改请求尚未找到")
    return DraftSaved(draft_id=row.draft_id, revision=row.applied_revision)
