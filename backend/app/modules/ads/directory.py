"""只展开已界定的账户集合；每次读取重新校验当前成员与 BC 授权。"""

from sqlalchemy import tuple_
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
    """单对象调用与同步批量发布共用同一名称审计规则。"""
    return append_campaign_name_projections(
        session, campaigns=((campaign_ref, raw_name),), parser_revision=parser_revision
    )[0]


def append_campaign_name_projections(
    session: Session,
    *,
    campaigns: tuple[tuple[EntityRef, str], ...],
    parser_revision: int,
) -> tuple[CampaignNameProjection, ...]:
    """按批锁定对象、读取最新审计并追加；备注变化只增加名称版本。"""
    if (
        type(parser_revision) is not int
        or parser_revision < 1
        or any(ref.kind != "campaign" for ref, _name in campaigns)
    ):
        raise DomainError("invalid_campaign_projection", "系列名称投影参数无效")
    identities = {
        (ref.tenant_id, ref.advertiser_id, ref.remote_id): name
        for ref, name in campaigns
    }
    result = []
    keys = sorted(identities)
    for start in range(0, len(keys), 500):
        batch = keys[start : start + 500]
        # 与独立调用者共用稳定对象行锁；按身份排序避免多个批次逆序拿锁。
        objects = session.exec(
            select(AdObject)
            .where(
                AdObject.kind == "campaign",
                tuple_(
                    col(AdObject.tenant_id),
                    col(AdObject.advertiser_id),
                    col(AdObject.remote_id),
                ).in_(batch),
            )
            .order_by(
                col(AdObject.tenant_id),
                col(AdObject.advertiser_id),
                col(AdObject.remote_id),
            )
            .with_for_update()
        ).all()
        if len(objects) != len(batch):
            raise DomainError("ad_object_not_found", "系列对象尚未同步")
        previous_rows = session.exec(
            select(CampaignNameProjection)
            .where(
                tuple_(
                    col(CampaignNameProjection.tenant_id),
                    col(CampaignNameProjection.advertiser_id),
                    col(CampaignNameProjection.campaign_remote_id),
                ).in_(batch),
            )
            .distinct(
                col(CampaignNameProjection.tenant_id),
                col(CampaignNameProjection.advertiser_id),
                col(CampaignNameProjection.campaign_remote_id),
            )
            .order_by(
                col(CampaignNameProjection.tenant_id),
                col(CampaignNameProjection.advertiser_id),
                col(CampaignNameProjection.campaign_remote_id),
                col(CampaignNameProjection.name_revision).desc(),
            )
        ).all()
        previous_by_key = {
            (p.tenant_id, p.advertiser_id, p.campaign_remote_id): p
            for p in previous_rows
        }
        for tenant_id, advertiser_id, remote_id in batch:
            raw_name = identities[tenant_id, advertiser_id, remote_id]
            identity = parse_campaign_name(raw_name)
            previous = previous_by_key.get((tenant_id, advertiser_id, remote_id))
            if previous and (previous.raw_name, previous.parser_revision) == (
                raw_name,
                parser_revision,
            ):
                result.append(previous)
                continue
            changed = previous is not None and (
                previous.provider_label,
                previous.drama_name,
                previous.status,
            ) != (identity.provider_label, identity.drama_name, identity.status)
            projection = CampaignNameProjection(
                tenant_id=tenant_id,
                advertiser_id=advertiser_id,
                campaign_remote_id=remote_id,
                raw_name=raw_name,
                provider_label=identity.provider_label,
                drama_name=identity.drama_name,
                status=identity.status,
                parser_revision=parser_revision,
                name_revision=previous.name_revision + 1 if previous else 1,
                grouping_revision=previous.grouping_revision + int(changed)
                if previous
                else 1,
            )
            session.add(projection)
            result.append(projection)
        session.flush()
    # 单条接口要求原身份对应的返回值；批量输入由调用方按对象身份去重。
    by_key = {(p.tenant_id, p.advertiser_id, p.campaign_remote_id): p for p in result}
    return tuple(
        by_key[ref.tenant_id, ref.advertiser_id, ref.remote_id] for ref, _ in campaigns
    )
