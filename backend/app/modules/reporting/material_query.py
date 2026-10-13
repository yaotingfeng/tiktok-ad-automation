"""素材日事实独立计费；广告使用位置只用于管理身份，不参与金额相加。

成员、筛选与排序由 row_set 在数据库内完成；这里只为冻结的素材行补全展示与指标。
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, or_, select

from app.integrations.tiktok.contracts.ads import MaterialUseRef
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.dimension_query import (
    COMPLETE_COVERAGE_STATUSES,
    _aggregate,
    _coverage,
    empty_metric_values,
    fact_vector,
    merge_metric_buckets,
    metric_vector,
    scoped_rows,
)
from app.modules.reporting.row_set import (
    MATERIAL_UNSUPPORTED,
    SnapshotMember,
    entity_ref,
)
from app.modules.reporting.schemas import ReportingFilter, ReportRow
from app.modules.reporting.store_models import MaterialReportDay, ReportSyncState


def _use(data: dict[str, Any]) -> MaterialUseRef:
    return MaterialUseRef(
        entity_ref(data["ad_ref"]),
        data["platform_material_id"],
        data.get("ad_material_id"),
        data["material_type"],
    )


def materialize_material_rows(
    session: Session,
    *,
    tenant_id: UUID,
    filters: ReportingFilter,
    members: Sequence[SnapshotMember],
) -> tuple[ReportRow, ...]:
    identities = {
        member.row_key: tuple(json.loads(member.row_key)[i] for i in (0, 2, 3))
        for member in members
    }
    advertisers = sorted({adv for adv, _, _ in identities.values()})
    material_ids = sorted({mid for _, mid, _ in identities.values()})
    accounts = {
        a.advertiser_id: a
        for a in scoped_rows(
            session, AdvertiserAccount, tenant_id=tenant_id, advertisers=advertisers
        )
    }
    candidates_by_key: dict[
        tuple[str, str, str | None], dict[MaterialUseRef, AdMaterialReference]
    ] = defaultdict(dict)
    for usage in session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == tenant_id,
            col(AdMaterialReference.advertiser_id).in_(advertisers),
            col(AdMaterialReference.complete).is_(True),
            or_(
                col(AdMaterialReference.main_material_id).in_(material_ids),
                col(AdMaterialReference.ad_material_id).in_(material_ids),
            ),
        )
    ).all():
        # 主素材为带类型身份；广告内素材 ID 只能关联同一账户，绝不能跨账户猜测。
        if usage.main_material_id:
            candidates_by_key[
                (usage.advertiser_id, usage.main_material_id, usage.main_material_type)
            ][usage.use_ref] = usage
        if usage.ad_material_id:
            candidates_by_key[(usage.advertiser_id, usage.ad_material_id, None)][
                usage.use_ref
            ] = usage
    states = session.exec(
        select(ReportSyncState).where(
            ReportSyncState.tenant_id == tenant_id,
            col(ReportSyncState.advertiser_id).in_(advertisers),
            ReportSyncState.report_kind == "material_day",
            ReportSyncState.stat_date >= filters.start_date,
            ReportSyncState.stat_date <= filters.end_date,
        )
    ).all()
    data = _aggregate(
        session,
        model=MaterialReportDay,
        group_cols=["advertiser_id", "main_material_id", "main_material_type"],
        tenant_id=tenant_id,
        advertisers=advertisers,
        start=filters.start_date,
        end=filters.end_date,
        ids=material_ids,
    )
    grouped = defaultdict(list)
    for item in data:
        grouped[
            (
                item["advertiser_id"],
                item["main_material_id"],
                item["main_material_type"],
            )
        ].append(item)
    # 覆盖状态和空值口径只取决于账户：每个账户只算一次。
    coverage_by_account = {
        adv: _coverage(states, advertisers={adv}, filters=filters, kind="material_day")
        for adv in advertisers
    }
    empty_by_account = {
        adv: empty_metric_values(
            states, advertiser=adv, filters=filters, kind="material_day"
        )
        for adv in advertisers
    }
    result = []
    for member in members:
        adv, mid, material_type = identities[member.row_key]
        items = grouped.get((adv, mid, material_type), [])
        candidates = candidates_by_key.get(
            (adv, mid, material_type), {}
        ) | candidates_by_key.get((adv, mid, None), {})
        uses = tuple(_use(item) for item in member.material_uses)
        refs = tuple(entity_ref(ref) for ref in member.refs)
        name = (
            next(
                (i["main_material_name"] for i in items if i.get("main_material_name")),
                None,
            )
            # 与快照排序同一取值：候选使用位置名称取最小者，保证展示与排序一致。
            or min((u.name for u in candidates.values() if u.name), default=None)
            or mid
        )
        coverage = dict(coverage_by_account[adv])
        if not uses:
            coverage["reason"] = "usage_unknown"
        result.append(
            ReportRow(
                row_key=member.row_key,
                display={
                    "name": name,
                    "material_name": name,
                    "main_material_id": mid,
                    "material_type": material_type,
                    "account_id": adv,
                    "advertiser_id": adv,
                    "account_name": accounts[adv].name or adv,
                },
                refs=refs,
                material_uses=uses,
                # 与其他维度同一规则：账户覆盖不完整时，已同步部分的合计标 PARTIAL。
                metric_buckets=merge_metric_buckets(
                    [
                        (
                            tuple(
                                fact_vector(item, unsupported=MATERIAL_UNSUPPORTED)
                                for item in items
                            )
                            or (
                                metric_vector(
                                    empty_by_account[adv],
                                    currency=accounts[adv].currency,
                                    timezone=accounts[adv].timezone,
                                    unsupported=MATERIAL_UNSUPPORTED,
                                ),
                            ),
                            coverage["status"] not in COMPLETE_COVERAGE_STATUSES,
                        )
                    ]
                ),
                coverage=coverage,
                membership_digest=sha256(
                    json.dumps(
                        [
                            (
                                u.ad_ref.remote_id,
                                u.platform_material_id,
                                u.ad_material_id,
                                u.material_type,
                            )
                            for u in uses
                        ],
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
                directory_versions={
                    str(u.id): u.published_version for u in candidates.values()
                },
            )
        )
    return tuple(result)
