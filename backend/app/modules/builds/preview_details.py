"""V2 分组摘要与广告分页；一条广告内的全部创意成员始终完整返回。"""

from uuid import UUID

from sqlalchemy import func, tuple_
from sqlmodel import col, select

from app.core.errors import DomainError
from app.core.pagination import Page, count_rows
from app.modules.accounts.resolver import decode_cursor, encode_cursor

from .creative_units import frozen_ad_creative_units
from .preview_material_views import GroupMaterialView
from .preview_materials import material_not_skipped
from .preview_models import (
    PlannedAd,
    PlannedGroup,
    PreviewCreativeUnitItem,
    PreviewGroupCreativeUnit,
    PreviewSkippedMaterial,
)
from .preview_schemas import FrozenAd, FrozenGroup


def group_summaries(session, *, context, preview, unit, cursor, limit):
    scope = {
        "kind": "frozen_groups_v2",
        "tenant": str(context.tenant_id),
        "preview": str(preview.id),
        "unit": str(unit.id),
    }
    after = decode_cursor(cursor, scope=scope)
    query = select(PlannedGroup).where(
        PlannedGroup.tenant_id == context.tenant_id,
        PlannedGroup.preview_id == preview.id,
        PlannedGroup.unit_id == unit.id,
    )
    total = count_rows(session, query)
    if after:
        query = query.where(PlannedGroup.group_no > int(after))
    groups = session.exec(
        query.order_by(col(PlannedGroup.group_no)).limit(limit + 1)
    ).all()
    items = []
    # 每页最多100组；每组只取计数及6个样例，不加载任何完整组内容。
    for group in groups[:limit]:
        material_query = select(GroupMaterialView.material_id).where(
            GroupMaterialView.tenant_id == context.tenant_id,
            GroupMaterialView.preview_id == preview.id,
            GroupMaterialView.drama_id == unit.drama_id,
            GroupMaterialView.group_no == group.group_no,
            material_not_skipped(
                tenant_id=context.tenant_id,
                unit_id=unit.id,
                material_id=col(GroupMaterialView.material_id),
            ),
        )
        count = count_rows(session, material_query)
        sample = session.exec(
            material_query.order_by(col(GroupMaterialView.position)).limit(6)
        ).all()
        unit_count = session.exec(
            select(func.count(func.distinct(PreviewGroupCreativeUnit.creative_unit_id)))
            .join(
                PreviewCreativeUnitItem,
                (
                    col(PreviewCreativeUnitItem.tenant_id)
                    == PreviewGroupCreativeUnit.tenant_id
                )
                & (
                    col(PreviewCreativeUnitItem.preview_id)
                    == PreviewGroupCreativeUnit.preview_id
                )
                & (
                    col(PreviewCreativeUnitItem.creative_unit_id)
                    == PreviewGroupCreativeUnit.creative_unit_id
                ),
            )
            .where(
                PreviewGroupCreativeUnit.tenant_id == context.tenant_id,
                PreviewGroupCreativeUnit.preview_id == preview.id,
                PreviewGroupCreativeUnit.drama_id == unit.drama_id,
                PreviewGroupCreativeUnit.group_no == group.group_no,
                material_not_skipped(
                    tenant_id=context.tenant_id,
                    unit_id=unit.id,
                    material_id=col(PreviewCreativeUnitItem.material_id),
                ),
            )
        ).one()
        ad_count = count_rows(
            session,
            select(PlannedAd).where(
                PlannedAd.tenant_id == context.tenant_id,
                PlannedAd.preview_id == preview.id,
                PlannedAd.group_id == group.id,
            ),
        )
        items.append(
            FrozenGroup(
                contract_version=2,
                group_id=group.id,
                group_no=group.group_no,
                name=group.name,
                material_ids=None,
                ads=None,
                material_count=count,
                creative_unit_count=unit_count,
                ad_count=ad_count,
                sample_material_ids=tuple(sample),
            )
        )
    return Page(
        items=items,
        total=total,
        next_cursor=encode_cursor(scope=scope, last_id=str(groups[limit - 1].group_no))
        if len(groups) > limit
        else None,
    )


def ad_page(session, *, context, preview, unit, group_id: UUID, cursor, limit):
    if type(limit) is not int or not 1 <= limit <= 5:
        raise DomainError("configuration_invalid", "广告详情每页最多5条")
    group = session.exec(
        select(PlannedGroup).where(
            PlannedGroup.tenant_id == context.tenant_id,
            PlannedGroup.preview_id == preview.id,
            PlannedGroup.unit_id == unit.id,
            PlannedGroup.id == group_id,
        )
    ).one_or_none()
    if group is None:
        raise DomainError("preview_not_found", "冻结分组不存在")
    scope = {
        "kind": "frozen_ads_v2",
        "tenant": str(context.tenant_id),
        "preview": str(preview.id),
        "unit": str(unit.id),
        "group": str(group.id),
    }
    after = decode_cursor(cursor, scope=scope)
    query = select(PlannedAd).where(
        PlannedAd.tenant_id == context.tenant_id,
        PlannedAd.preview_id == preview.id,
        PlannedAd.group_id == group.id,
    )
    total = count_rows(session, query)
    if after:
        base, creative = map(int, after.split(":"))
        query = query.where(
            tuple_(col(PlannedAd.base_ad_no), col(PlannedAd.creative_no))
            > (base, creative)
        )
    rows = session.exec(
        query.order_by(col(PlannedAd.base_ad_no), col(PlannedAd.creative_no)).limit(
            limit + 1
        )
    ).all()
    items = []
    for ad in rows[:limit]:
        args = {
            "tenant_id": context.tenant_id,
            "preview_id": preview.id,
            "drama_id": unit.drama_id,
            "group_no": group.group_no,
            "base_ad_no": ad.base_ad_no,
        }
        creatives = frozen_ad_creative_units(session, **args)
        skipped = (
            set(
                session.exec(
                    select(PreviewSkippedMaterial.material_id).where(
                        PreviewSkippedMaterial.tenant_id == context.tenant_id,
                        PreviewSkippedMaterial.unit_id == unit.id,
                        col(PreviewSkippedMaterial.material_id).in_(
                            [m.material_id for c in creatives for m in c.members]
                        ),
                    )
                ).all()
            )
            if creatives[0].kind == "VIDEO"
            else set()
        )
        creatives = tuple(
            c
            for c in creatives
            if c.kind == "CAROUSEL" or c.members[0].material_id not in skipped
        )
        material_ids = [m.material_id for c in creatives for m in c.members]
        items.append(
            FrozenAd(
                ad_id=ad.id,
                base_ad_no=ad.base_ad_no,
                creative_no=ad.creative_no,
                name=ad.name,
                copy_id=ad.copy_id,
                text=ad.text,
                cta_option_ids=tuple(ad.cta_option_ids),
                material_ids=tuple(material_ids),
                creative_units=creatives,
            )
        )
    last = rows[limit - 1] if len(rows) > limit else None
    return Page(
        items=items,
        total=total,
        next_cursor=encode_cursor(
            scope=scope, last_id=f"{last.base_ad_no}:{last.creative_no}"
        )
        if last
        else None,
    )
