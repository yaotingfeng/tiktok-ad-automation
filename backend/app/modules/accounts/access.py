from uuid import UUID

from sqlalchemy import and_, func
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.schemas import AccountAccess
from app.modules.tenants.permissions import require_tenant

# Fail closed for unknown provider values even if a stale grant claims capability.
OPERABLE_REMOTE_STATUSES = frozenset({"STATUS_ENABLE", "ENABLE"})


def usable_grants(
    *, tenant_id: UUID, bc_id: str, action: str
) -> SelectOfScalar[BCAccountAccess]:
    """One joined statement, also used for efficient deterministic source choice."""
    statement = (
        select(BCAccountAccess)
        .join(
            TikTokConnection,
            and_(
                col(TikTokConnection.tenant_id) == BCAccountAccess.tenant_id,
                col(TikTokConnection.id) == BCAccountAccess.connection_id,
            ),
        )
        .join(
            TenantBC,
            and_(
                col(TenantBC.tenant_id) == BCAccountAccess.tenant_id,
                col(TenantBC.bc_id) == BCAccountAccess.bc_id,
            ),
        )
        .join(
            AdvertiserAccount,
            and_(
                col(AdvertiserAccount.tenant_id) == BCAccountAccess.tenant_id,
                col(AdvertiserAccount.advertiser_id) == BCAccountAccess.advertiser_id,
            ),
        )
        .where(
            BCAccountAccess.tenant_id == tenant_id,
            BCAccountAccess.bc_id == bc_id,
            col(BCAccountAccess.in_bc).is_(True),
            col(BCAccountAccess.authorized).is_(True),
            col(BCAccountAccess.active).is_(True),
            TikTokConnection.status == "ACTIVE",
            col(TenantBC.ownership_conflict).is_(False),
            col(AdvertiserAccount.ownership_conflict).is_(False),
            func.trim(col(AdvertiserAccount.currency)) != "",
            func.trim(col(AdvertiserAccount.timezone)) != "",
        )
    )
    if action != "read":
        statement = statement.where(
            BCAccountAccess.permission_state == "VERIFIED",
            col(AdvertiserAccount.remote_status).in_(OPERABLE_REMOTE_STATUSES),
        )
    if action == "build":
        statement = statement.where(col(BCAccountAccess.can_build).is_(True))
    if action == "upload":
        statement = statement.where(col(BCAccountAccess.can_upload).is_(True))
    return statement


def resolve_account_access(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    action: str,
) -> AccountAccess:
    if action not in {"read", "build", "upload"}:
        raise DomainError("invalid_account_action", "账户动作无效")
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
    )
    account = session.get(
        AdvertiserAccount, (context.tenant_id, advertiser_id), populate_existing=True
    )
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if not account or not bc:
        raise DomainError("account_not_in_bc", "账户不在当前租户 BC 目录")
    if account.ownership_conflict or bc.ownership_conflict:
        raise DomainError("account_ownership_conflict", "账户或 BC 归属冲突")
    if not account.currency.strip() or not account.timezone.strip():
        raise DomainError("account_metadata_incomplete", "账户信息尚未完整")
    grant = session.exec(
        usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action=action)
        .where(BCAccountAccess.advertiser_id == advertiser_id)
        .order_by(col(BCAccountAccess.connection_id))
        .limit(1)
        .execution_options(populate_existing=True)
    ).first()
    if grant is None:
        raise DomainError("account_access_denied", "当前授权不支持该账户操作")
    return AccountAccess(
        advertiser_id=advertiser_id,
        bc_id=bc_id,
        connection_id=grant.connection_id,
        currency=account.currency,
        timezone=account.timezone,
    )


def assign_upload_account(
    session: Session, *, context: TenantContext, bc_id: str
) -> AccountAccess:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="upload"
    )
    grant = session.exec(
        usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="upload")
        .order_by(
            col(BCAccountAccess.advertiser_id), col(BCAccountAccess.connection_id)
        )
        .limit(1)
        .execution_options(populate_existing=True)
    ).first()
    if grant is None:
        raise DomainError("no_upload_account", "当前 BC 没有可上传的授权账户")
    # Return actual selected source/connection. Upload tasks persist these facts;
    # this selection does not mutate a permanent material-account preference.
    return resolve_account_access(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=grant.advertiser_id,
        action="upload",
    )
