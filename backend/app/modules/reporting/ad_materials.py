"""广告详情里的素材：这条广告下每个素材在所选日期的数据，以及只针对本广告的启停选择。

数据取自素材日报（含收入），经 material_post_link 归到本广告：Spark 帖子只属于一条
广告，数据就是这个素材在本广告里的表现。非 Spark 主素材可能被多条广告共用，平台
不拆分，这类行标 shared，金额是共用广告的合计。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads import directory
from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.dimension_query import (
    COMPLETE_COVERAGE_STATUSES,
    _aggregate,
    _coverage,
    empty_metric_values,
    fact_vector,
    merge_metric_buckets,
    metric_vector,
)
from app.modules.reporting.query_models import FrozenSelectionRecord
from app.modules.reporting.row_set import MATERIAL_UNSUPPORTED
from app.modules.reporting.schemas import (
    FrozenSelection,
    MetricVector,
    ReportingFilter,
)
from app.modules.reporting.selection import selection_digest, selection_public
from app.modules.reporting.store_models import MaterialReportDay, ReportSyncState

MAX_DAY_SPAN = 90
SELECTION_TTL = timedelta(minutes=15)


class AdMaterialPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    use_ref: MaterialUseRef
    name: str = ""
    operation_status: str | None = None
    # 报表里的主素材 ID（Spark 为帖子 ID）；未出现在素材日报时为空。
    post_ids: tuple[str, ...] = ()
    # 主素材被多条广告共用：平台不拆分，金额是这些广告的合计。
    shared: bool = False
    metric_buckets: tuple[MetricVector, ...] = ()


class AdMaterialsPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[AdMaterialPublic, ...] = ()
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


def _ad_uses(session: Session, ref: EntityRef) -> list[AdMaterialReference]:
    """本广告的素材，每个视频一条：目录可能另有不带广告内素材 ID 的重复记录，
    有带 ID 的记录时只保留它（只有它能单独启停）。"""
    rows = session.exec(
        select(AdMaterialReference)
        .where(
            AdMaterialReference.tenant_id == ref.tenant_id,
            AdMaterialReference.advertiser_id == ref.advertiser_id,
            AdMaterialReference.ad_remote_id == ref.remote_id,
            col(AdMaterialReference.complete).is_(True),
        )
        .order_by(
            col(AdMaterialReference.name), col(AdMaterialReference.ad_material_id)
        )
    ).all()
    with_id = {row.platform_material_id for row in rows if row.ad_material_id}
    return [
        row
        for row in rows
        if row.ad_material_id or row.platform_material_id not in with_id
    ]


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
    uses = _ad_uses(session, ref)
    links = session.connection().execute(
        text("""
        SELECT l.platform_material_id AS vid, l.main_material_id,
            (SELECT count(*) FROM material_post_link o
              WHERE o.tenant_id = l.tenant_id AND o.advertiser_id = l.advertiser_id
                AND o.main_material_id = l.main_material_id) > 1 AS shared
        FROM material_post_link l
        WHERE l.tenant_id = :t AND l.advertiser_id = :a AND l.ad_remote_id = :ad
        """),
        {"t": context.tenant_id, "a": advertiser_id, "ad": ad_id},
    )
    # 按视频归集本广告的帖子：素材重建后同一视频在本广告可能有新旧多条帖子。
    posts: dict[str | None, list[str]] = defaultdict(list)
    shared: set[str] = set()
    for row in links:
        posts[row.vid].append(row.main_material_id)
        if row.shared:
            shared.add(row.main_material_id)
    post_ids = sorted({mid for mids in posts.values() for mid in mids})
    facts = defaultdict(list)
    if post_ids:
        for item in _aggregate(
            session,
            model=MaterialReportDay,
            group_cols=["advertiser_id", "main_material_id", "main_material_type"],
            tenant_id=context.tenant_id,
            advertisers=[advertiser_id],
            start=start,
            end=end,
            ids=post_ids,
        ):
            facts[item["main_material_id"]].append(item)
    filters = ReportingFilter(dimension="material", start_date=start, end_date=end)
    states = session.exec(
        select(ReportSyncState).where(
            ReportSyncState.tenant_id == context.tenant_id,
            ReportSyncState.advertiser_id == advertiser_id,
            ReportSyncState.report_kind == "material_day",
            ReportSyncState.stat_date >= start,
            ReportSyncState.stat_date <= end,
        )
    ).all()
    coverage = _coverage(
        states, advertisers={advertiser_id}, filters=filters, kind="material_day"
    )
    incomplete = coverage["status"] not in COMPLETE_COVERAGE_STATUSES
    account = session.get(AdvertiserAccount, (context.tenant_id, advertiser_id))
    empty = empty_metric_values(
        states, advertiser=advertiser_id, filters=filters, kind="material_day"
    )
    items = []
    for use in uses:
        mids = sorted(posts.get(use.platform_material_id, []))
        vectors = tuple(
            fact_vector(item, unsupported=MATERIAL_UNSUPPORTED)
            for mid in mids
            for item in facts.get(mid, [])
        )
        if not vectors and account is not None:
            # 无数据的素材按账户覆盖填 0 或空，与报表其他维度一致。
            vectors = (
                metric_vector(
                    empty,
                    currency=account.currency,
                    timezone=account.timezone,
                    unsupported=MATERIAL_UNSUPPORTED,
                ),
            )
        items.append(
            AdMaterialPublic(
                use_ref=use.use_ref,
                name=use.name,
                operation_status=use.operation_status,
                post_ids=tuple(mids),
                shared=any(mid in shared for mid in mids),
                metric_buckets=merge_metric_buckets([(vectors, incomplete)]),
            )
        )
    return AdMaterialsPublic(items=tuple(items), coverage=coverage)


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
    by_id = {use.ad_material_id: use for use in _ad_uses(session, ref)}
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
