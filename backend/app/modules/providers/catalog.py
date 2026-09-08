"""Read-only historical links and SQL aggregates, independent of BC selection."""

from typing import Literal
from uuid import UUID

from sqlalchemy import and_, func, or_
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import Select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.providers.models import (
    LinkPreparationItem,
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
)
from app.modules.providers.repository import _after_id, _cursor, _limit, _preparation
from app.modules.providers.schemas import (
    PreparationSummary,
    ProviderLinkPublic,
    display_config,
)
from app.modules.tenants.permissions import require_tenant

LinkStatus = Literal[
    "pending", "ready", "superseded", "invalid", "result_unknown", "failed"
]
PENDING_STATUSES = frozenset(
    {"pending", "resolving", "checking", "creating", "verifying"}
)


def _link_query(
    tenant_id: UUID,
) -> Select[
    tuple[PromotionLink, ProviderDrama, ProviderConnection, ProviderApplication]
]:
    return (
        select(PromotionLink, ProviderDrama, ProviderConnection, ProviderApplication)
        .join(
            ProviderDrama,
            and_(
                col(ProviderDrama.tenant_id) == PromotionLink.tenant_id,
                col(ProviderDrama.id) == PromotionLink.drama_id,
            ),
        )
        .join(
            ProviderConnection,
            and_(
                col(ProviderConnection.tenant_id) == PromotionLink.tenant_id,
                col(ProviderConnection.id) == PromotionLink.connection_id,
            ),
        )
        .join(
            ProviderApplication,
            and_(
                col(ProviderApplication.tenant_id) == PromotionLink.tenant_id,
                col(ProviderApplication.connection_id) == PromotionLink.connection_id,
                col(ProviderApplication.external_id) == PromotionLink.application_id,
            ),
        )
        .where(PromotionLink.tenant_id == tenant_id)
    )


def _public(
    link: PromotionLink,
    drama: ProviderDrama,
    connection: ProviderConnection,
    app: ProviderApplication,
) -> ProviderLinkPublic:
    config = display_config(link.config)
    return ProviderLinkPublic(
        link_id=link.id,
        drama_id=drama.id,
        external_drama_id=drama.external_drama_id,
        title=drama.title,
        language=drama.language,
        provider_kind=connection.kind,
        connection_id=connection.id,
        connection_name=connection.display_name,
        connection_status=connection.status,
        application_id=app.external_id,
        application_name=app.name,
        tiktok_minis_id=app.tiktok_minis_id,
        status=link.status,
        version=link.version,
        url=link.url,
        protected_base=link.protected_base,
        verified_at=link.verified_at,
        config=config,
        config_display_incomplete=config != link.config,
    )


def list_links(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID | None = None,
    application_id: str | None = None,
    query: str = "",
    status: LinkStatus | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[ProviderLinkPublic]:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    _limit(limit)
    if len(query) > 1000 or limit not in {50, 100}:
        raise DomainError("provider_request_invalid", "筛选或页长无效")
    scope = {
        "kind": "provider-links",
        "tenant_id": str(context.tenant_id),
        "connection_id": str(connection_id) if connection_id else None,
        "application_id": application_id,
        "query": query,
        "status": status,
    }
    after = _after_id(cursor, scope)
    statement = _link_query(context.tenant_id)
    if connection_id:
        statement = statement.where(PromotionLink.connection_id == connection_id)
    if application_id:
        statement = statement.where(PromotionLink.application_id == application_id)
    if status:
        statement = statement.where(PromotionLink.status == status)
    if query.strip():
        statement = statement.where(
            or_(
                col(ProviderDrama.title).icontains(query.strip(), autoescape=True),
                col(ProviderDrama.external_drama_id) == query.strip(),
            )
        )
    if after:
        statement = statement.where(PromotionLink.id > after)
    rows = session.exec(
        statement.order_by(col(PromotionLink.id))
        .limit(limit + 1)
        .execution_options(populate_existing=True)
    ).all()
    return Page(
        items=[_public(*row) for row in rows[:limit]],
        next_cursor=_cursor(scope, rows[limit - 1][0].id)
        if len(rows) > limit
        else None,
    )


def get_link(
    session: Session, *, context: TenantContext, link_id: UUID
) -> ProviderLinkPublic:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    row = session.exec(
        _link_query(context.tenant_id)
        .where(PromotionLink.id == link_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "推广链接不存在")
    return _public(*row)


def preparation_summary(
    session: Session, *, context: TenantContext, task_id: UUID
) -> PreparationSummary:
    prep = _preparation(session, context, task_id)
    connection = session.get(
        ProviderConnection, prep.connection_id, populate_existing=True
    )
    assert connection is not None
    app = session.exec(
        select(ProviderApplication).where(
            ProviderApplication.tenant_id == context.tenant_id,
            ProviderApplication.connection_id == prep.connection_id,
            ProviderApplication.external_id == prep.application_id,
        )
    ).one()
    rows = session.exec(
        select(LinkPreparationItem.status, func.count())
        .where(
            LinkPreparationItem.tenant_id == context.tenant_id,
            LinkPreparationItem.preparation_id == prep.id,
        )
        .group_by(col(LinkPreparationItem.status))
    ).all()
    counts: dict[str, int] = {}
    for status, count in rows:
        key = "pending" if status in PENDING_STATUSES else status
        counts[key] = counts.get(key, 0) + count
    total, ready, pending = (
        sum(counts.values()),
        counts.get("ready", 0),
        counts.get("pending", 0),
    )
    config = display_config(prep.config)
    return PreparationSummary(
        task_id=prep.id,
        connection_id=connection.id,
        connection_name=connection.display_name,
        provider_kind=connection.kind,
        application_id=app.external_id,
        application_name=app.name,
        status=prep.status,
        config=config,
        config_display_incomplete=config != prep.config,
        total_count=total,
        ready_count=ready,
        pending_count=pending,
        exception_count=total - ready - pending,
        counts=counts,
    )
