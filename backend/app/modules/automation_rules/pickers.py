"""规则编辑器的账户与对象选择器：只返回本租户可读的账户及其已同步对象。"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.automation_rules.inputs import expand_accounts
from app.modules.automation_rules.schemas import AccountScope
from app.modules.automation_rules.service import require

ObjectLevel = Literal["campaign", "adgroup", "ad", "material"]


@dataclass(frozen=True)
class AccountOption:
    advertiser_id: str
    name: str
    bc_id: str
    currency: str
    timezone: str


@dataclass(frozen=True)
class ObjectOption:
    # 与规则定义中的指定对象格式一致：「账户ID:远端ID」，素材为「账户ID:主素材ID」。
    value: str
    label: str
    advertiser_id: str
    kind: str
    status: str | None


def _accounts(session: Session, context: TenantContext) -> dict[str, AccountOption]:
    infos = expand_accounts(
        session, tenant_id=context.tenant_id, scope=AccountScope(mode="ALL")
    )
    return {
        adv: AccountOption(adv, info.name, info.bc_id, info.currency, info.timezone)
        for adv, info in infos.items()
    }


def search_accounts(
    session: Session, *, context: TenantContext, query: str, limit: int
) -> list[AccountOption]:
    require(session, context, "read")
    text = query.strip().lower()
    options = [
        option
        for option in _accounts(session, context).values()
        if not text
        or text in option.advertiser_id.lower()
        or text in option.name.lower()
    ]
    return options[:limit]


def search_objects(
    session: Session,
    *,
    context: TenantContext,
    level: ObjectLevel,
    query: str,
    advertiser_ids: list[str],
    limit: int,
    offset: int = 0,
) -> list[ObjectOption]:
    require(session, context, "read")
    allowed = set(_accounts(session, context))
    if advertiser_ids:
        allowed &= set(advertiser_ids)
    if not allowed:
        return []
    pattern = f"%{query.strip()}%"
    if level == "material":
        rows = session.exec(
            select(
                AdMaterialReference.advertiser_id,
                AdMaterialReference.main_material_id,
                func.max(AdMaterialReference.name),
                # 素材在任一广告中开启即视为开启，与判断引擎一致。
                func.max(AdMaterialReference.operation_status),
            )
            .where(
                AdMaterialReference.tenant_id == context.tenant_id,
                col(AdMaterialReference.advertiser_id).in_(allowed),
                col(AdMaterialReference.main_material_id).is_not(None),
                or_(
                    col(AdMaterialReference.name).ilike(pattern),
                    col(AdMaterialReference.main_material_id).ilike(pattern),
                ),
            )
            .group_by(
                col(AdMaterialReference.advertiser_id),
                col(AdMaterialReference.main_material_id),
            )
            .order_by(
                col(AdMaterialReference.advertiser_id),
                col(AdMaterialReference.main_material_id),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return [
            ObjectOption(f"{adv}:{main}", name or str(main), adv, "material", status)
            for adv, main, name, status in rows
        ]
    objects = session.exec(
        select(AdObject)
        .where(
            AdObject.tenant_id == context.tenant_id,
            AdObject.kind == level,
            col(AdObject.advertiser_id).in_(allowed),
            col(AdObject.operation_status) != "DELETE",
            or_(
                col(AdObject.name).ilike(pattern),
                col(AdObject.remote_id).ilike(pattern),
            ),
        )
        .order_by(
            col(AdObject.advertiser_id), col(AdObject.name), col(AdObject.remote_id)
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        ObjectOption(
            f"{row.advertiser_id}:{row.remote_id}",
            row.name,
            row.advertiser_id,
            row.kind,
            row.operation_status,
        )
        for row in objects
    ]
