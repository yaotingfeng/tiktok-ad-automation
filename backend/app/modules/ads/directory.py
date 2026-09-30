"""只展开已界定的账户集合；每次读取重新校验当前成员与 BC 授权。"""

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import ENTITY_KINDS, EntityRef
from app.modules.accounts.access import usable_grants
from app.modules.accounts.models import BCAccountAccess
from app.modules.ads.models import AdObject, CampaignNameProjection
from app.modules.ads.naming import parse_campaign_name
from app.modules.tenants.permissions import require_tenant


def _authorize(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_ids: tuple[str, ...],
) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    if not advertiser_ids or len(set(advertiser_ids)) != len(advertiser_ids):
        raise DomainError("invalid_directory_scope", "请先界定要读取的账户集合")
    grants = session.exec(
        usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="read")
        .where(col(BCAccountAccess.advertiser_id).in_(advertiser_ids))
        .execution_options(populate_existing=True)
    ).all()
    if {grant.advertiser_id for grant in grants} != set(advertiser_ids):
        raise DomainError("directory_forbidden", "当前 BC 没有这些账户的有效读取授权")


def locate(
    session: Session, *, context: TenantContext, bc_id: str, ref: EntityRef
) -> AdObject:
    if ref.tenant_id != context.tenant_id:
        raise DomainError("directory_forbidden", "无法读取其他租户的投放对象")
    _authorize(
        session, context=context, bc_id=bc_id, advertiser_ids=(ref.advertiser_id,)
    )
    row = session.get(
        AdObject,
        (ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id),
        populate_existing=True,
    )
    if row is None:
        raise DomainError("ad_object_not_found", "投放对象尚未同步")
    return row


def list_objects(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_ids: tuple[str, ...],
    kind: str,
    parent: EntityRef | None = None,
) -> tuple[AdObject, ...]:
    if kind not in ENTITY_KINDS or (
        parent is not None
        and (
            parent.tenant_id != context.tenant_id
            or parent.advertiser_id not in advertiser_ids
        )
    ):
        raise DomainError("invalid_directory_scope", "对象类型或父对象范围无效")
    _authorize(session, context=context, bc_id=bc_id, advertiser_ids=advertiser_ids)
    statement = select(AdObject).where(
        AdObject.tenant_id == context.tenant_id,
        col(AdObject.advertiser_id).in_(advertiser_ids),
        AdObject.kind == kind,
    )
    if parent is not None:
        statement = statement.where(
            AdObject.advertiser_id == parent.advertiser_id,
            AdObject.parent_kind == parent.kind,
            AdObject.parent_remote_id == parent.remote_id,
        )
    return tuple(
        session.exec(
            statement.order_by(
                AdObject.advertiser_id, AdObject.remote_id
            ).execution_options(populate_existing=True)
        ).all()
    )


def append_campaign_name_projection(
    session: Session, *, campaign_ref: EntityRef, raw_name: str, parser_revision: int
) -> CampaignNameProjection:
    """发布事务内追加名称审计；备注/空白变化不导致经营归组版本跳变。"""
    if (
        campaign_ref.kind != "campaign"
        or type(parser_revision) is not int
        or parser_revision < 1
    ):
        raise DomainError("invalid_campaign_projection", "系列名称投影参数无效")
    identity = parse_campaign_name(raw_name)
    # 锁定稳定对象行，避免两个发布者并发分配同一 name_revision。
    row = session.exec(
        select(AdObject)
        .where(
            AdObject.tenant_id == campaign_ref.tenant_id,
            AdObject.advertiser_id == campaign_ref.advertiser_id,
            AdObject.kind == "campaign",
            AdObject.remote_id == campaign_ref.remote_id,
        )
        .with_for_update()
    ).first()
    if row is None:
        raise DomainError("ad_object_not_found", "系列对象尚未同步")
    previous = session.exec(
        select(CampaignNameProjection)
        .where(
            CampaignNameProjection.tenant_id == campaign_ref.tenant_id,
            CampaignNameProjection.advertiser_id == campaign_ref.advertiser_id,
            CampaignNameProjection.campaign_remote_id == campaign_ref.remote_id,
        )
        .order_by(col(CampaignNameProjection.name_revision).desc())
        .limit(1)
    ).first()
    if previous and (previous.raw_name, previous.parser_revision) == (
        raw_name,
        parser_revision,
    ):
        return previous
    grouping_revision = 1
    if previous:
        changed = (previous.provider_label, previous.drama_name, previous.status) != (
            identity.provider_label,
            identity.drama_name,
            identity.status,
        )
        grouping_revision = previous.grouping_revision + int(changed)
    projection = CampaignNameProjection(
        tenant_id=campaign_ref.tenant_id,
        advertiser_id=campaign_ref.advertiser_id,
        campaign_remote_id=campaign_ref.remote_id,
        raw_name=raw_name,
        provider_label=identity.provider_label,
        drama_name=identity.drama_name,
        status=identity.status,
        parser_revision=parser_revision,
        name_revision=previous.name_revision + 1 if previous else 1,
        grouping_revision=grouping_revision,
    )
    session.add(projection)
    session.flush()
    return projection
