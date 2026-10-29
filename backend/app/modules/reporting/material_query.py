"""素材维度行补全：一个素材库视频一行，跨账户、跨广告合计帖子事实。

成员、筛选与排序由 row_set 在数据库内完成；这里只为冻结的素材行补全展示与指标。
帖子到视频的归属见 material_links。
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.integrations.tiktok.contracts.ads import MaterialUseRef
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.dimension_query import (
    COMPLETE_COVERAGE_STATUSES,
    _aggregate,
    _coverage,
    any_of,
    empty_metric_values,
    fact_vector,
    merge_metric_buckets,
    metric_vector,
    scoped_rows,
)
from app.modules.reporting.material_links import material_row_key, post_video_map
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
    advertisers: Sequence[str],
) -> tuple[ReportRow, ...]:
    """为冻结的素材行补全展示与指标；行身份与 row_set._material_rows 一致。

    视频行合计该视频在全部账户、全部广告的帖子事实；帖子行只合计这一条帖子。
    """
    scope = sorted(set(advertisers))
    video_of = post_video_map(
        session,
        tenant_id=tenant_id,
        advertisers=scope,
        start=filters.start_date,
        end=filters.end_date,
    )
    keys = {member.row_key: json.loads(member.row_key) for member in members}
    videos = {key[1] for key in keys.values() if key[0] == "material"}
    # 归属必须带事实类型；同 MID 的未知类型仍保留原始帖子行。
    posts_by_row: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    post_ids = sorted(
        {mid for (adv, mid, mtype), vid in video_of.items() if vid in videos}
        | {key[2] for key in keys.values() if key[0] == "post"}
    )
    usages = (
        session.exec(
            select(AdMaterialReference).where(
                AdMaterialReference.tenant_id == tenant_id,
                col(AdMaterialReference.advertiser_id).in_(scope),
                # 必须写成布尔列本身才能命中部分索引 ix_ad_material_video（WHERE complete）；
                # 写成 IS true 规划器不认，会退回逐账户扫描全部使用记录。
                col(AdMaterialReference.complete),
                AdMaterialReference.material_type == "VIDEO",
                any_of(col(AdMaterialReference.platform_material_id), videos),
            )
        ).all()
        if videos
        else []
    )
    usage_by_video: dict[str, list[AdMaterialReference]] = defaultdict(list)
    for usage in usages:
        usage_by_video[usage.platform_material_id].append(usage)
    data = (
        _aggregate(
            session,
            model=MaterialReportDay,
            group_cols=["advertiser_id", "main_material_id", "main_material_type"],
            tenant_id=tenant_id,
            advertisers=scope,
            start=filters.start_date,
            end=filters.end_date,
            ids=post_ids,
        )
        if post_ids
        else []
    )
    facts: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in data:
        identity = (
            item["advertiser_id"],
            item["main_material_id"],
            item["main_material_type"],
        )
        row_key = material_row_key(identity[0], identity[1], video_of.get(identity))
        if row_key in keys:
            posts_by_row[row_key].add(identity)
            facts[identity].append(item)
    accounts = {
        a.advertiser_id: a
        for a in scoped_rows(
            session, AdvertiserAccount, tenant_id=tenant_id, advertisers=scope
        )
    }
    states = session.exec(
        select(ReportSyncState).where(
            ReportSyncState.tenant_id == tenant_id,
            col(ReportSyncState.advertiser_id).in_(scope),
            ReportSyncState.report_kind == "material_day",
            ReportSyncState.stat_date >= filters.start_date,
            ReportSyncState.stat_date <= filters.end_date,
        )
    ).all()
    # 覆盖只取决于账户与日期：每个账户算一次，不随行和事实重复扫描同步状态。
    incomplete_by_adv = {
        adv: _coverage(states, advertisers={adv}, filters=filters, kind="material_day")[
            "status"
        ]
        not in COMPLETE_COVERAGE_STATUSES
        for adv in scope
    }
    empty_by_adv = {
        adv: empty_metric_values(
            states, advertiser=adv, filters=filters, kind="material_day"
        )
        for adv in accounts
    }
    result = []
    for member in members:
        key = keys[member.row_key]
        posts = posts_by_row.get(member.row_key, set())
        row_usages = usage_by_video.get(key[1], []) if key[0] == "material" else []
        # 行涉及的账户与快照 row_advs 一致：有事实的账户 ∪ 使用位置所在账户。
        row_advs = sorted(
            {adv for adv, _, _ in posts}
            | {u.advertiser_id for u in row_usages}
            | {use.ad_ref.advertiser_id for use in map(_use, member.material_uses)}
        )
        row_advs = [adv for adv in row_advs if adv in accounts]
        items = [item for post in sorted(posts) for item in facts.get(post, [])]
        coverage = _coverage(
            states, advertisers=set(row_advs), filters=filters, kind="material_day"
        )
        uses = tuple(_use(item) for item in member.material_uses)
        if not uses:
            coverage["reason"] = "usage_unknown"
        fact_advs = {item["advertiser_id"] for item in items}
        # 账户覆盖不完整时，该账户已同步部分的金额标 PARTIAL（与其他维度同一规则）。
        buckets = [
            (
                (fact_vector(item, unsupported=MATERIAL_UNSUPPORTED),),
                incomplete_by_adv[item["advertiser_id"]],
            )
            for item in items
        ] + [
            # 没有事实的账户按覆盖填 0 或空，与快照排序口径一致。
            (
                (
                    metric_vector(
                        empty_by_adv[adv],
                        currency=accounts[adv].currency,
                        timezone=accounts[adv].timezone,
                        unsupported=MATERIAL_UNSUPPORTED,
                    ),
                ),
                incomplete_by_adv[adv],
            )
            for adv in row_advs
            if adv not in fact_advs
        ]
        mid = key[1] if key[0] == "material" else key[2]
        # 与快照排序同一取值：目录名优先（各帖子一致），否则取素材日报名称。
        name = (
            min((u.name for u in row_usages if u.name), default=None)
            or min(
                (i["main_material_name"] for i in items if i.get("main_material_name")),
                default=None,
            )
            or mid
        )
        single = row_advs[0] if len(row_advs) == 1 else None
        result.append(
            ReportRow(
                row_key=member.row_key,
                display={
                    "name": name,
                    "material_name": name,
                    "main_material_id": mid,
                    "material_type": (
                        items[0].get("main_material_type") if items else None
                    )
                    or (row_usages[0].material_type if row_usages else None),
                    "account_id": single,
                    "advertiser_id": single,
                    "account_name": (accounts[single].name or single)
                    if single
                    else None,
                    # 跨账户合计的视频行没有单一账户；页面用账户数提示合计范围。
                    "account_count": str(len(row_advs)),
                },
                refs=tuple(entity_ref(ref) for ref in member.refs),
                material_uses=uses,
                metric_buckets=merge_metric_buckets(buckets),
                coverage=coverage,
                membership_digest=sha256(
                    json.dumps(
                        [
                            (
                                u.ad_ref.advertiser_id,
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
                directory_versions={str(u.id): u.published_version for u in row_usages},
            )
        )
    return tuple(result)
