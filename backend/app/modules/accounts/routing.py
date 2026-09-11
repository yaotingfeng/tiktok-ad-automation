"""只从明确 BC 默认冻结连接；后续任务只核验原连接，不重新选路。"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlmodel import Session, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import (
    OPERABLE_REMOTE_STATUSES,
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant

Capability = Literal["read", "upload", "build"]


def _bc(session: Session, context: TenantContext, bc_id: str) -> TenantBC:
    row = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if row is None:
        raise DomainError("account_not_in_bc", "当前租户没有该 BC")
    if row.ownership_conflict:
        raise DomainError("account_ownership_conflict", "BC 归属冲突")
    return row


def _connection(
    session: Session, context: TenantContext, bc_id: str, connection_id: UUID
) -> TikTokConnection:
    connection = session.get(TikTokConnection, connection_id, populate_existing=True)
    if connection is None or connection.tenant_id != context.tenant_id:
        raise DomainError("connection_not_found", "当前租户连接不存在")
    binding = session.get(
        BCConnectionBinding,
        (context.tenant_id, bc_id, connection_id),
        populate_existing=True,
    )
    if binding is None or binding.kind != connection.kind:
        raise DomainError("connection_bc_mismatch", "连接未绑定当前 BC")
    if connection.status != "ACTIVE":
        raise DomainError("connection_unavailable", "连接当前不可用")
    return connection


def freeze_route(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    connection_id: UUID | None = None,
) -> FrozenTikTokRoute:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    _bc(session, context, bc_id)
    if connection_id is None:
        default = session.get(
            BCDefaultRoute, (context.tenant_id, bc_id), populate_existing=True
        )
        if default is None:
            raise DomainError(
                "bc_default_connection_required", "请先为该 BC 选择默认执行连接"
            )
        connection_id = default.connection_id
    connection = _connection(session, context, bc_id, connection_id)
    return FrozenTikTokRoute(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        connection_id=connection.id,
        channel=connection.kind,
        authorization_revision=connection.authorization_revision,
        adapter_contract_revision=connection.adapter_contract_revision,
    )


def _fresh(observed_at: datetime | None, now: datetime) -> bool:
    if observed_at is None or observed_at.tzinfo is None:
        return False
    age = (now - observed_at).total_seconds()
    return 0 <= age <= settings.BC_CAPABILITY_MAX_AGE_SECONDS


def verify_route(
    session: Session,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    advertiser_id: str | None,
    capability: Capability,
) -> None:
    if capability not in {"read", "upload", "build"}:
        raise DomainError("invalid_account_action", "账户动作无效")
    if route.tenant_id != context.tenant_id:
        raise DomainError("connection_tenant_mismatch", "连接不属于当前租户")
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action=capability,
    )
    if advertiser_id is None and capability != "read":
        raise DomainError("account_required", "该操作必须指定广告账户")
    if advertiser_id is not None and (
        type(advertiser_id) is not str or not advertiser_id.strip()
    ):
        raise DomainError("account_required", "该操作必须指定广告账户")
    _bc(session, context, route.bc_id)
    connection = _connection(session, context, route.bc_id, route.connection_id)
    if connection.kind != route.channel:
        raise DomainError("connection_channel_mismatch", "连接通道已改变")
    if connection.authorization_revision != route.authorization_revision:
        raise DomainError("route_authorization_changed", "原任务的授权范围已改变")
    if connection.adapter_contract_revision != route.adapter_contract_revision:
        raise DomainError("route_contract_changed", "原任务的接口契约已改变")
    # BC 目录/角色读取正用于更新账户证据；不能先要求待刷新的账户证据新鲜。
    # gateway 操作白名单负责将 None 限定为这些读取及协议请求。
    if advertiser_id is None:
        return
    account = session.get(
        AdvertiserAccount,
        (context.tenant_id, advertiser_id),
        populate_existing=True,
    )
    grant = session.get(
        BCAccountAccess,
        (context.tenant_id, route.bc_id, advertiser_id, route.connection_id),
        populate_existing=True,
    )
    if account is None or grant is None:
        raise DomainError("account_not_in_bc", "账户不在该连接的 BC 目录")
    if account.ownership_conflict:
        raise DomainError("account_ownership_conflict", "账户归属冲突")
    if not account.currency.strip() or not account.timezone.strip():
        raise DomainError("account_metadata_incomplete", "账户信息尚未完整")
    if not (grant.in_bc and grant.authorized and grant.active):
        raise DomainError("account_access_denied", "当前连接未授权该账户")
    authorization = session.exec(
        select(ConnectionAuthorization)
        .where(
            ConnectionAuthorization.tenant_id == context.tenant_id,
            ConnectionAuthorization.connection_id == route.connection_id,
            ConnectionAuthorization.authorization_revision
            == route.authorization_revision,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    now = datetime.now(UTC)
    if (
        authorization is None
        or not _fresh(authorization.verified_at, now)
        or not _fresh(grant.checked_at, now)
    ):
        raise DomainError("route_evidence_stale", "账户授权证据需要重新检查")
    if (
        not authorization.source
        or authorization.source == "UNKNOWN"
        or authorization.permission_summary.get(f"{capability}_authorized") is not True
    ):
        raise DomainError("account_access_denied", "缺少该操作的明确授权证据")
    if capability != "read" and (
        not authorization.scopes
        or grant.permission_state != "VERIFIED"
        or account.remote_status not in OPERABLE_REMOTE_STATUSES
        or (capability == "upload" and not grant.can_upload)
        or (capability == "build" and not grant.can_build)
    ):
        raise DomainError("account_access_denied", "账户当前角色或范围不支持该操作")


def set_default_route(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    connection_id: UUID,
) -> FrozenTikTokRoute:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    # BC 行是设置默认的互斥点；一笔事务中验证并写入同一条明确绑定。
    row = session.exec(
        select(TenantBC)
        .where(TenantBC.tenant_id == context.tenant_id, TenantBC.bc_id == bc_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise DomainError("account_not_in_bc", "当前租户没有该 BC")
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    route = freeze_route(
        session, context=context, bc_id=bc_id, connection_id=connection_id
    )
    default = session.get(BCDefaultRoute, (context.tenant_id, bc_id))
    if default is None:
        default = BCDefaultRoute(
            tenant_id=context.tenant_id, bc_id=bc_id, connection_id=connection_id
        )
    else:
        default.connection_id = connection_id
    session.add(default)
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="tiktok.bc.default_connection",
            target_id=bc_id,
            details={"connection_id": str(connection_id), "channel": route.channel},
        )
    )
    session.flush()
    return route
