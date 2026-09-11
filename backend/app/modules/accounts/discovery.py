"""Transactional directory mutations; no network is performed under these locks."""

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.tenants.permissions import require_tenant

from .models import (
    AuthorizationAttempt,
    DiscoveryRun,
    ExternalAssetOwner,
    TikTokConnection,
)


def claim_external_asset(
    session: Session, *, kind: str, external_id: str, tenant_id: UUID
) -> bool:
    session.exec(
        insert(ExternalAssetOwner)
        .values(kind=kind, external_id=external_id, owner_tenant_id=tenant_id)
        .on_conflict_do_nothing(index_elements=["kind", "external_id"])
    )
    owner = session.get(ExternalAssetOwner, (kind, external_id), populate_existing=True)
    assert owner is not None
    return owner.owner_tenant_id == tenant_id


def locked_run(session: Session, run_id: UUID) -> DiscoveryRun:
    identity = session.get(DiscoveryRun, run_id)
    if identity is None:
        raise DomainError("discovery_not_found", "未找到发现任务")
    session.exec(
        select(TikTokConnection)
        .where(TikTokConnection.id == identity.connection_id)
        .with_for_update()
    ).one()
    run = session.exec(
        select(DiscoveryRun)
        .where(DiscoveryRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if run is None:
        raise DomainError("discovery_not_found", "未找到发现任务")
    return run


def validate_run(session: Session, run: DiscoveryRun) -> TikTokConnection:
    require_tenant(
        session, actor_id=run.actor_id, tenant_id=run.tenant_id, action="manage"
    )
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.id == run.connection_id,
            TikTokConnection.tenant_id == run.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if connection is None or connection.status == "DISABLED":
        raise DomainError("connection_unavailable", "当前租户连接不可用")
    if connection.credential_revision != run.credential_revision:
        raise DomainError("discovery_stale", "发现任务凭据版本已过期")
    if run.candidate_attempt_id:
        attempt = session.get(
            AuthorizationAttempt, run.candidate_attempt_id, populate_existing=True
        )
        if (
            attempt is None
            or attempt.tenant_id != run.tenant_id
            or attempt.connection_id != run.connection_id
            or attempt.actor_id != run.actor_id
            or attempt.status != "CANDIDATE_READY"
            or attempt.base_credential_revision != run.credential_revision
            or not attempt.candidate_ciphertext
        ):
            raise DomainError("discovery_stale", "候选授权已过期")
    elif connection.status != "ACTIVE":
        raise DomainError("connection_unavailable", "当前租户连接不可用")
    return connection


def finalize_directory(session: Session, *, run_id: UUID) -> None:
    """仅完整 staging 可发布；保留既有入口名，不保留按页修改 live grant 的行为。"""
    from .api_directory import publish_api_directory

    run = session.get(DiscoveryRun, run_id)
    if run is None:
        raise DomainError("discovery_not_found", "未找到发现任务")
    publish_api_directory(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="tenant_admin"
        ),
        run_id=run_id,
    )


def publish_mcp_directory(
    session: Session, *, context: TenantContext, run_id: UUID
) -> None:
    """MCP 暂存完成后在同一事务发布，绝不复用按页修改 live grant 的 API 路径。"""
    from .mcp_discovery import publish_mcp_directory as publish

    publish(session, context=context, run_id=run_id)
