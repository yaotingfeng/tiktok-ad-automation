"""广告详情：本广告的创意经济事实，以及精确当前素材位置的管理动作。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.integrations.tiktok.contracts.carousel import (
    CarouselCreativeIdentity,
    CarouselDirectory,
)
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads import directory
from app.modules.ads.directory import directory_carousels
from app.modules.reporting.carousel_links import (
    FrozenCarouselLink,
    frozen_carousel_links,
)
from app.modules.reporting.creative_query import (
    UNALLOCATED_NOTE,
    creative_states,
    read_facts,
    read_unallocated,
)
from app.modules.reporting.dimension_query import (
    COMPLETE_COVERAGE_STATUSES,
    _coverage,
    empty_metric_values,
    merge_metric_buckets,
    metric_vector,
)
from app.modules.reporting.material_query import materialize_material_rows
from app.modules.reporting.query_models import FrozenSelectionRecord
from app.modules.reporting.row_set import SnapshotMember
from app.modules.reporting.schemas import (
    FrozenSelection,
    MetricVector,
    ReportingFilter,
    ReportRow,
)
from app.modules.reporting.selection import selection_digest, selection_public
from app.modules.reporting.store_models import METRIC_COLUMNS

MAX_DAY_SPAN = 90
SELECTION_TTL = timedelta(minutes=15)


class AdMaterialPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    use_ref: MaterialUseRef
    name: str = ""
    operation_status: str | None = None
    creative_ids: tuple[str, ...] = ()
    metric_buckets: tuple[MetricVector, ...] = ()


class AdMaterialsPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[AdMaterialPublic, ...] = ()
    carousel_creatives: tuple[CarouselCreativeIdentity, ...] = ()
    carousel_linkage_complete: bool = True
    metric_scope: Literal["CREATIVE"] = "CREATIVE"
    creative_rows: tuple[ReportRow, ...] = ()
    frozen_creative_links: tuple[FrozenCarouselLink, ...] = ()
    coverage: dict[str, object] = {}


def _locate_ad(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    ad_id: str,
) -> EntityRef:
    ref = EntityRef(context.tenant_id, advertiser_id, "ad", ad_id)
    try:
        directory.locate(session, context=context, bc_id=bc_id, ref=ref)
    except DomainError as exc:
        # 不暴露其他 BC 或租户的对象是否存在。
        raise HTTPException(404, detail="ad_not_found") from exc
    return ref


def list_ad_materials(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    ad_id: str,
    start: date,
    end: date,
) -> AdMaterialsPublic:
    if end < start or (end - start).days + 1 > MAX_DAY_SPAN:
        raise HTTPException(422, detail="ad_materials_range_invalid")
    ref = _locate_ad(
        session, context=context, bc_id=bc_id, advertiser_id=advertiser_id, ad_id=ad_id
    )
    entity = directory.locate(session, context=context, bc_id=bc_id, ref=ref)
    carousel = directory_carousels(entity)
    current_uses = directory.current_ad_materials(session, ref)
    if not carousel.is_carousel and any(
        use.material_type == "IMAGE" for use in current_uses
    ):
        # 目录主响应缺内容时只能确认图片格式，不以扁平使用记录猜顺序和曲目。
        carousel = CarouselDirectory(is_carousel=True, complete=False)
    uses = [use for use in current_uses if use.material_type != "IMAGE"]
    filters = ReportingFilter(
        dimension="material", start_date=start, end_date=end, ids=(ad_id,)
    )
    facts = read_facts(
        session,
        tenant_id=context.tenant_id,
        advertisers=[advertiser_id],
        filters=filters,
        exact_ad=ad_id,
    )
    states = creative_states(
        session,
        tenant_id=context.tenant_id,
        advertisers=[advertiser_id],
        start=start,
        end=end,
    )
    coverage = _coverage(
        states, advertisers={advertiser_id}, filters=filters, kind="creative_material"
    )
    missing = read_unallocated(
        session,
        tenant_id=context.tenant_id,
        advertisers=[advertiser_id],
        filters=filters,
        exact_ad=ad_id,
    )
    if missing:
        coverage.update(
            status="INCOMPLETE",
            unallocated_note=UNALLOCATED_NOTE,
            unallocated_group_count=len(
                {(m["adgroup_id"], m["currency"], m["timezone"]) for m in missing}
            ),
        )
    incomplete = coverage["status"] not in COMPLETE_COVERAGE_STATUSES
    account = session.get(AdvertiserAccount, (context.tenant_id, advertiser_id))
    empty = empty_metric_values(
        states, advertiser=advertiser_id, filters=filters, kind="creative_material"
    )
    items = []
    for use in uses:
        # 不把共享 VID 的另一条广告金额带入本位置，也不把轮播金额分给图片。
        items_for_use = [
            f
            for f in facts
            if f["material_type"] == use.material_type
            and f["platform_material_id"] == use.platform_material_id
            and f["ad_material_id"] == use.ad_material_id
            and f["ad_remote_id"] == ad_id
        ]
        vectors = tuple(
            metric_vector(
                f,
                currency=f["currency"],
                timezone=f["timezone"],
                partial={m for m in METRIC_COLUMNS if f[f"{m}_has_null"]},
            )
            for f in items_for_use
        )
        # 缺口的币种/时区也独立保留，不能借已有另一日金额把空桶吞掉。
        vectors += tuple(
            metric_vector(
                dict.fromkeys(METRIC_COLUMNS), currency=currency, timezone=timezone
            )
            for currency, timezone in {(m["currency"], m["timezone"]) for m in missing}
            - {(f["currency"], f["timezone"]) for f in items_for_use}
        )
        if not vectors and account is not None:
            # 无数据且无未分摊缺口时，按账户覆盖填 0 或空。
            vectors = (
                metric_vector(
                    empty, currency=account.currency, timezone=account.timezone
                ),
            )
        items.append(
            AdMaterialPublic(
                use_ref=use.use_ref,
                name=use.name,
                operation_status=use.operation_status,
                creative_ids=tuple(sorted({f["creative_id"] for f in items_for_use})),
                metric_buckets=merge_metric_buckets(
                    [
                        (
                            vectors,
                            incomplete
                            or any(
                                f["source_status"] != "COMPLETE" or f["unallocated"]
                                for f in items_for_use
                            ),
                        )
                    ]
                ),
            )
        )
    return AdMaterialsPublic(
        items=tuple(items),
        coverage=coverage,
        carousel_creatives=carousel.identities,
        carousel_linkage_complete=carousel.complete,
        creative_rows=materialize_material_rows(
            session,
            exact_ad=ad_id,
            tenant_id=context.tenant_id,
            advertisers=[advertiser_id],
            filters=filters,
            members=[
                SnapshotMember(key, (), ())
                for key in sorted({f["row_key"] for f in facts})
            ],
        ),
        frozen_creative_links=frozen_carousel_links(
            session,
            tenant_id=context.tenant_id,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            ad_id=ad_id,
        )
        if carousel.is_carousel
        else (),
    )


def freeze_ad_material_selection(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    ad_id: str,
    ad_material_ids: tuple[str, ...],
) -> FrozenSelection:
    """只冻结这条广告里的指定素材；预览、提交走广告管理的同一流程。"""
    if not ad_material_ids or len(set(ad_material_ids)) != len(ad_material_ids):
        raise HTTPException(422, detail="ad_material_selection_invalid")
    ref = _locate_ad(
        session, context=context, bc_id=bc_id, advertiser_id=advertiser_id, ad_id=ad_id
    )
    by_id = {
        use.ad_material_id: use for use in directory.current_ad_materials(session, ref)
    }
    if not set(ad_material_ids) <= set(by_id):
        raise HTTPException(404, detail="ad_material_not_found")
    uses = [by_id[item].use_ref for item in ad_material_ids]
    material_uses = [
        {
            "ad_ref": {
                "tenant_id": str(use.ad_ref.tenant_id),
                "advertiser_id": use.ad_ref.advertiser_id,
                "kind": use.ad_ref.kind,
                "remote_id": use.ad_ref.remote_id,
            },
            "platform_material_id": use.platform_material_id,
            "ad_material_id": use.ad_material_id,
            "material_type": use.material_type,
        }
        for use in uses
    ]
    filters = {"source": "ad_detail_materials", "ad_id": ad_id}
    now = datetime.now(UTC)
    record = FrozenSelectionRecord(
        id=uuid4(),
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        actor_id=context.actor_id,
        created_at=now,
        expires_at=now + SELECTION_TTL,
        # 不来自报表快照；只保留来源 ID 字段，冻结目标不依赖快照存活。
        snapshot_id=uuid4(),
        advertiser_ids=[advertiser_id],
        filters=filters,
        filter_digest=hashlib.sha256(
            json.dumps(filters, sort_keys=True).encode()
        ).hexdigest(),
        publication_versions={},
        naming_versions={},
        refs=[],
        material_uses=material_uses,
        membership_digest=selection_digest((), tuple(uses)),
    )
    session.add(record)
    session.flush()
    return selection_public(record)
