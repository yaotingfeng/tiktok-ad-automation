"""Scoped reads for provider modules; no network calls or implicit commits.

Preparation creation and effect claims are implemented by their later workflow
steps. These reads always reload tenant membership and constrain every identity
by tenant/connection/application, including when a caller already holds an ORM
instance in the Session identity map.
"""

import base64
import binascii
import json
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.providers.models import (
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
    ProviderEffect,
)
from app.modules.providers.schemas import link_reuse_key
from app.modules.tenants.permissions import require_tenant


def get_connection(
    session: Session, *, context: TenantContext, connection_id: UUID
) -> ProviderConnection:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    result = session.exec(
        select(ProviderConnection)
        .where(
            ProviderConnection.tenant_id == context.tenant_id,
            ProviderConnection.id == connection_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if result is None:
        raise DomainError("resource_not_found", "当前租户版权方连接不存在")
    return result


def get_application(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID,
    application_id: str,
) -> ProviderApplication:
    get_connection(session, context=context, connection_id=connection_id)
    result = session.exec(
        select(ProviderApplication)
        .where(
            ProviderApplication.tenant_id == context.tenant_id,
            ProviderApplication.connection_id == connection_id,
            ProviderApplication.external_id == application_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if result is None:
        raise DomainError("resource_not_found", "当前连接版权方应用不存在")
    return result


def _cursor(scope: dict[str, Any], last_id: UUID) -> str:
    payload = json.dumps(
        {"scope": scope, "last_id": str(last_id)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _after_id(cursor: str | None, scope: dict[str, Any]) -> UUID | None:
    if cursor is None:
        return None
    try:
        if len(cursor) > 8192:
            raise ValueError("cursor length")
        raw = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(raw, dict)
            or set(raw) != {"scope", "last_id"}
            or raw["scope"] != scope
        ):
            raise ValueError("cursor scope")
        return UUID(raw["last_id"])
    except ValueError, TypeError, KeyError, AttributeError, binascii.Error:
        raise DomainError("invalid_cursor", "游标不属于当前版权方查询") from None


def _limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise DomainError("configuration_invalid", "分页数量必须在 1 到 200 之间")


def list_dramas(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID,
    application_id: str,
    title: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> Page[ProviderDrama]:
    """Exact local title matches, with a stable scope-bound UUID seek cursor."""
    get_application(
        session,
        context=context,
        connection_id=connection_id,
        application_id=application_id,
    )
    _limit(limit)
    scope = {
        "tenant_id": str(context.tenant_id),
        "connection_id": str(connection_id),
        "application_id": application_id,
        "title": title,
    }
    after_id = _after_id(cursor, scope)
    statement = select(ProviderDrama).where(
        ProviderDrama.tenant_id == context.tenant_id,
        ProviderDrama.connection_id == connection_id,
        ProviderDrama.application_id == application_id,
    )
    if title is not None:
        statement = statement.where(ProviderDrama.title == title)
    if after_id is not None:
        statement = statement.where(col(ProviderDrama.id) > after_id)
    rows = list(
        session.exec(
            statement.order_by(col(ProviderDrama.id))
            .limit(limit + 1)
            .execution_options(populate_existing=True)
        ).all()
    )
    return Page[ProviderDrama](
        items=rows[:limit],
        next_cursor=_cursor(scope, rows[limit - 1].id) if len(rows) > limit else None,
    )


def find_ready_link(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID,
    application_id: str,
    external_drama_id: str,
    config: dict[str, Any],
) -> PromotionLink | None:
    get_application(
        session,
        context=context,
        connection_id=connection_id,
        application_id=application_id,
    )
    key = link_reuse_key(
        context.tenant_id, connection_id, application_id, external_drama_id, config
    )
    # Join the recorded identity as well as the key, so a malformed/stale key in a
    # stored row cannot make another drama appear reusable under this request.
    return session.exec(
        select(PromotionLink)
        .join(
            ProviderDrama,
            (
                (col(PromotionLink.tenant_id) == col(ProviderDrama.tenant_id))
                & (col(PromotionLink.connection_id) == col(ProviderDrama.connection_id))
                & (
                    col(PromotionLink.application_id)
                    == col(ProviderDrama.application_id)
                )
                & (col(PromotionLink.drama_id) == col(ProviderDrama.id))
            ),
        )
        .where(
            PromotionLink.tenant_id == context.tenant_id,
            PromotionLink.connection_id == connection_id,
            PromotionLink.application_id == application_id,
            PromotionLink.reuse_key == key,
            PromotionLink.status == "ready",
            col(PromotionLink.config) == config,
            ProviderDrama.external_drama_id == external_drama_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()


# Workflow helpers only flush; orchestration owns short transaction boundaries.
def claim_remote_scope(
    session: Session, *, tenant_id: UUID, scope_key: str, item_id: UUID
) -> bool:
    from sqlalchemy.dialects.postgresql import insert

    from app.modules.providers.models import ProviderRemoteScope

    session.exec(
        insert(ProviderRemoteScope)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            scope_key=scope_key,
            status="idle",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "scope_key"])
    )
    scope = session.exec(
        select(ProviderRemoteScope)
        .where(
            ProviderRemoteScope.tenant_id == tenant_id,
            ProviderRemoteScope.scope_key == scope_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if scope.active_item_id is not None and scope.active_item_id != item_id:
        return False
    if scope.active_item_id is None:
        scope.active_item_id = item_id
        scope.status = "held"
        session.add(scope)
        session.flush()
    return True


def get_or_create_effect(
    session: Session, *, tenant_id: UUID, scope_key: str, step: str, request_digest: str
) -> ProviderEffect:
    from sqlalchemy.dialects.postgresql import insert

    from app.modules.providers.models import ProviderEffect

    session.exec(
        insert(ProviderEffect)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            remote_scope_key=scope_key,
            step=step,
            request_digest=request_digest,
            status="pending",
            result={},
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "remote_scope_key", "step", "request_digest"]
        )
    )
    return session.exec(
        select(ProviderEffect)
        .where(
            ProviderEffect.tenant_id == tenant_id,
            ProviderEffect.remote_scope_key == scope_key,
            ProviderEffect.step == step,
            ProviderEffect.request_digest == request_digest,
        )
        .execution_options(populate_existing=True)
    ).one()
