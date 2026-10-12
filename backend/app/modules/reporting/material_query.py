"""素材日事实独立计费；广告使用位置只用于管理身份，不参与金额相加。"""

from __future__ import annotations

import json
from collections import defaultdict
from hashlib import sha256

from sqlmodel import col, select

from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.models import AdMaterialReference, AdObject, CampaignNameProjection
from app.modules.reporting.dimension_query import (
    COMPLETE_COVERAGE_STATUSES,
    _aggregate,
    _coverage,
    _entity_matches,
    _hierarchy_display,
    _passes_filter,
    _sort_rows,
    empty_metric_values,
    latest_projections,
    merge_metric_buckets,
    metric_vector,
    scoped_rows,
)
from app.modules.reporting.filters import authorized_grants, compile_filter
from app.modules.reporting.schemas import ReportRow
from app.modules.reporting.store_models import MaterialReportDay, ReportSyncState


def build_material_rows(session, *, context, bc_id, filters) -> tuple[ReportRow, ...]:
    compile_filter(filters)
    advertisers = {
        g.advertiser_id
        for g in authorized_grants(
            session, context=context, bc_id=bc_id, advertiser_ids=filters.advertiser_ids
        )
    }
    accounts = {
        a.advertiser_id: a
        for a in scoped_rows(
            session,
            AdvertiserAccount,
            tenant_id=context.tenant_id,
            advertisers=advertisers,
        )
    }
    objects = {
        (o.advertiser_id, o.kind, o.remote_id): o
        for o in scoped_rows(
            session, AdObject, tenant_id=context.tenant_id, advertisers=advertisers
        )
    }
    projections = latest_projections(
        scoped_rows(
            session,
            CampaignNameProjection,
            tenant_id=context.tenant_id,
            advertisers=advertisers,
        )
    )
    usage_index = defaultdict(dict)
    directory_materials = set()
    for usage in scoped_rows(
        session,
        AdMaterialReference,
        tenant_id=context.tenant_id,
        advertisers=advertisers,
    ):
        if not usage.complete:
            continue
        material_id = usage.main_material_id or usage.ad_material_id
        material_type = usage.main_material_type or usage.material_type
        if material_id and material_type:
            directory_materials.add((usage.advertiser_id, material_id, material_type))
        # 主素材为带类型身份；广告内素材 ID 只能关联同一账户，绝不能跨账户猜测。
        if usage.main_material_id:
            usage_index[
                (usage.advertiser_id, usage.main_material_id, usage.main_material_type)
            ][usage.use_ref] = usage
        if usage.ad_material_id:
            usage_index[(usage.advertiser_id, usage.ad_material_id, None)][
                usage.use_ref
            ] = usage
    states = session.exec(
        select(ReportSyncState).where(
            ReportSyncState.tenant_id == context.tenant_id,
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
        tenant_id=context.tenant_id,
        advertisers=advertisers,
        start=filters.start_date,
        end=filters.end_date,
    )
    # 覆盖状态和空值口径只取决于账户：每个账户只算一次。逐素材行重算会对
    # 全部同步状态做数万次全扫描，素材维度查询耗时数十秒、连接因空闲被代理断开。
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
    grouped = defaultdict(list)
    for item in data:
        grouped[
            (
                item["advertiser_id"],
                item["main_material_id"],
                item["main_material_type"],
            )
        ].append(item)
    for identity in directory_materials:
        grouped.setdefault(identity, [])
    result = []
    for (adv, mid, material_type), items in sorted(grouped.items()):
        candidates = usage_index.get((adv, mid, material_type), {}) | usage_index.get(
            (adv, mid, None), {}
        )
        uses = []
        hierarchy = []
        for usage in candidates.values():
            entity = objects.get((adv, "ad", usage.ad_remote_id))
            display = _hierarchy_display(
                accounts=accounts,
                objects=objects,
                advertiser_id=adv,
                kind="ad",
                remote_id=usage.ad_remote_id,
                entity=entity,
            )
            projection = projections.get((adv, display.get("campaign_id")))
            if _entity_matches(entity, projection, filters, accounts[adv].timezone):
                uses.append(usage.use_ref)
                hierarchy.append(display)
        if not uses and not _entity_matches(
            None, None, filters, accounts[adv].timezone
        ):
            continue
        uses = tuple(
            sorted(
                set(uses),
                key=lambda u: (
                    u.ad_ref.remote_id,
                    u.platform_material_id,
                    u.ad_material_id or "",
                    u.material_type,
                ),
            )
        )
        refs = tuple(dict.fromkeys(u.ad_ref for u in uses))
        name = (
            next(
                (i["main_material_name"] for i in items if i.get("main_material_name")),
                None,
            )
            or next((u.name for u in candidates.values() if u.name), None)
            or mid
        )
        display = {
            "name": name,
            "material_name": name,
            "main_material_id": mid,
            "material_type": material_type,
            "account_id": adv,
            "advertiser_id": adv,
            "account_name": accounts[adv].name or adv,
        }
        coverage = dict(coverage_by_account[adv])
        if not uses:
            coverage["reason"] = "usage_unknown"
        row = ReportRow(
            row_key=json.dumps([adv, "material", mid, material_type]),
            display=display,
            refs=refs,
            material_uses=uses,
            # 与其他维度同一规则：账户覆盖不完整时，已同步部分的合计标 PARTIAL。
            metric_buckets=merge_metric_buckets(
                [
                    (
                        tuple(
                            metric_vector(
                                item,
                                currency=item["currency"],
                                timezone=item["zone_min"]
                                if item["zone_min"] == item["zone_max"]
                                else "MIXED",
                                unsupported={"revenue_d3", "revenue_d30"},
                            )
                            for item in items
                        )
                        or (
                            metric_vector(
                                empty_by_account[adv],
                                currency=accounts[adv].currency,
                                timezone=accounts[adv].timezone,
                                unsupported={"revenue_d3", "revenue_d30"},
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
        # 搜索/层级筛选可命中使用位置，但金额仍是整份素材日报，不能随使用次数重复。
        if _passes_filter(row, filters) or any(
            _passes_filter(row.model_copy(update={"display": h | display}), filters)
            for h in hierarchy
        ):
            result.append(row)
    return _sort_rows(result, filters)
