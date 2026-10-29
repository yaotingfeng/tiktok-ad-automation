"""广告内素材输入：报表帖子归到广告内视频，执行只引用当前唯一使用身份。"""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from itertools import groupby
from uuid import UUID

from sqlmodel import Session, col, select

from app.modules.ads.directory import latest_ad_materials
from app.modules.ads.models import AdMaterialReference
from app.modules.automation_rules.inputs import (
    AccountInfo,
    Entity,
    in_rule_scope,
    local_window,
)
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.automation_rules.statuses import known_status
from app.modules.reporting.material_value import material_window_values
from app.modules.reporting.metric_catalog import DERIVED_RATIOS, STORED_KEYS
from app.modules.reporting.store_models import MaterialPostLink

VIDEO_REPORT_TYPES = ("VIDEO_SPARK_ADS", "VIDEO_NON_SPARK_ADS")


def material_use_id(ad_id: str, video_id: str, material_type: str) -> str:
    """广告内视频的稳定身份；广告内素材 ID 重建不改变规则频控/选择身份。"""
    raw = json.dumps([ad_id, video_id, material_type], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _parents_enabled(ad: Entity, objects: dict[str, Entity]) -> bool | None:
    current = ad
    for kind in ("ad", "adgroup", "campaign"):
        if current.kind != kind or known_status(current.status) is None:
            return None
        if current.status != "ENABLE":
            return False
        if kind != "campaign":
            parent = objects.get(current.parent_key or "")
            if parent is None:
                # 上级缺失须单列缺数，不能当成已关闭后从告警中悄悄排除。
                return None
            current = parent
    return True


def load_material_entities(
    session: Session,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    ads: dict[str, Entity],
    *,
    definition: RuleDefinition | None = None,
    now: datetime | None = None,
    ignored_posts: list[dict[str, str]] | None = None,
) -> dict[str, Entity]:
    if not accounts:
        return {}
    links = session.exec(
        select(MaterialPostLink).where(
            MaterialPostLink.tenant_id == tenant_id,
            col(MaterialPostLink.main_material_type).in_(VIDEO_REPORT_TYPES),
            col(MaterialPostLink.advertiser_id).in_(list(accounts)),
        )
    ).all()
    ranges = (
        {str(c.range) for c in definition.conditions if c.range}
        if definition
        else set()
    )
    windows = (
        [
            (adv, *local_window(r, info.timezone, now))
            for adv, info in accounts.items()
            for r in sorted(ranges)
        ]
        if now is not None
        else []
    )
    values = (
        material_window_values(
            session, tenant_id, windows, material_types=VIDEO_REPORT_TYPES
        )
        if windows
        else {}
    )
    by_video = defaultdict(list)
    owners = defaultdict(set)
    unresolved_ads = set()
    for link in links:
        owners[link.advertiser_id, link.main_material_id].add(
            (link.ad_remote_id, link.platform_material_id)
        )
        if not link.platform_material_id:
            # 只有窗口合计有金额的帖子身份缺口会影响止损；零金额曝光不能阻断广告。
            if values.get((link.advertiser_id, link.main_material_id), False):
                unresolved_ads.add((link.advertiser_id, link.ad_remote_id))
        else:
            by_video[
                link.advertiser_id, link.ad_remote_id, link.platform_material_id
            ].append(link)
    unlinked_accounts = unlinked_report_accounts(values, set(owners))
    if ignored_posts is not None:
        mapped = {
            (link.advertiser_id, link.main_material_id)
            for link in links
            if link.platform_material_id
        }
        counts = Counter(
            adv
            for (adv, mid), valuable in values.items()
            if not valuable and (adv, mid) not in mapped
        )
        # 按账户+帖子去重，不按事实日期/关联行/规则条件重复计数；仅留审计说明，不报缺数。
        ignored_posts.extend(
            {
                "advertiser_id": adv,
                "reason": "ZERO_VALUE_UNLINKED_POSTS_IGNORED",
                "count": str(count),
            }
            for adv, count in sorted(counts.items())
        )
    # 按广告顺序分批读取，当前广告选完身份后释放历史ORM行；不要把整个租户
    # 数万条素材及旧身份同时装进 by_ad。完整广告组仍交给共用选择器判定歧义。
    rows = session.exec(
        select(AdMaterialReference)
        .where(
            AdMaterialReference.tenant_id == tenant_id,
            col(AdMaterialReference.advertiser_id).in_(list(accounts)),
            col(AdMaterialReference.complete).is_(True),
            # 素材级规则仅有视频经济事实；图片和帖子身份不能借同名 ID 进入。
            AdMaterialReference.material_type == "VIDEO",
        )
        .order_by(AdMaterialReference.advertiser_id, AdMaterialReference.ad_remote_id)
        .execution_options(yield_per=256)
    )
    entities = {}
    for (adv, ad_id), candidates in groupby(
        rows, key=lambda row: (row.advertiser_id, row.ad_remote_id)
    ):
        ad = ads.get(f"ad:{adv}:{ad_id}")
        if ad is None:
            continue
        info = accounts[adv]
        parents_enabled = _parents_enabled(ad, ads)
        for use in latest_ad_materials(list(candidates)):
            refs = by_video[adv, ad_id, use.platform_material_id]
            issue = None
            if not use.ad_material_id or use.operation_status is None:
                issue = "MATERIAL_IDENTITY_AMBIGUOUS"
            elif parents_enabled is None:
                issue = "MATERIAL_PARENT_UNKNOWN"
            elif adv in unlinked_accounts:
                issue = "MATERIAL_REPORT_UNLINKED"
            elif (adv, ad_id) in unresolved_ads:
                # 同一广告有尚不能定位视频的旧帖子，不能少算历史消耗后作止损。
                issue = "MATERIAL_LINK_UNRESOLVED"
            elif not refs:
                issue = "MATERIAL_LINK_MISSING"
            elif any(len(owners[adv, link.main_material_id]) != 1 for link in refs):
                # 主素材被多条广告共用时，现有事实是合计，不能冒充单广告数据。
                issue = "MATERIAL_SHARED_REPORT"
            rid = material_use_id(ad_id, use.platform_material_id, use.material_type)
            key = f"material:{adv}:{rid}"
            entities[key] = Entity(
                key=key,
                kind="material",
                advertiser_id=adv,
                bc_id=info.bc_id,
                remote_id=rid,
                name=f"{use.name or use.platform_material_id} · {ad.name}",
                drama=ad.drama,
                naming_valid=ad.naming_valid,
                status=use.operation_status,
                currency=info.currency,
                timezone=info.timezone,
                parent_key=ad.key,
                attributes={
                    **{
                        key: value
                        for key, value in ad.attributes.items()
                        if key.endswith("_status")
                    },
                    "material_status": known_status(use.operation_status),
                    "name": use.name or use.platform_material_id,
                },
                material_uses=(
                    {
                        "advertiser_id": adv,
                        "ad_remote_id": ad_id,
                        "platform_material_id": use.platform_material_id,
                        "ad_material_id": use.ad_material_id,
                        "material_type": use.material_type,
                    },
                ),
                video=use.platform_material_id,
                report_ids=tuple(sorted({link.main_material_id for link in refs})),
                judgement_issue=issue,
                parents_enabled=parents_enabled,
            )
    return entities


def material_facts(
    entities: dict[str, Entity],
    posts: dict[str, dict[str, Decimal | None]],
) -> dict[str, dict[str, Decimal | None]]:
    """每笔帖子金额只归到它唯一的广告内视频；未知/共享数据不参与判断。"""
    result = {}
    for entity in entities.values():
        if entity.kind != "material" or entity.judgement_issue:
            continue
        rows = [
            posts[f"material:{entity.advertiser_id}:{pid}"]
            for pid in entity.report_ids
            if f"material:{entity.advertiser_id}:{pid}" in posts
        ]
        if rows:
            # 任一有事实的帖子缺指标时保留 NULL，不能用其他帖子的金额掩盖缺数。
            result[entity.key] = {
                key: sum((row[key] or Decimal(0) for row in rows), Decimal(0))
                if all(row.get(key) is not None for row in rows)
                else None
                for key in STORED_KEYS
            }
    return result


def material_issues(
    entities: dict[str, Entity], definition: RuleDefinition
) -> list[dict[str, str]]:
    """分账户记录部分素材无法判断的原因；沿用运行现有 JSON 字段，无需迁移。"""
    counts: Counter[tuple[str, str]] = Counter()
    for entity in entities.values():
        if entity.kind != "material" or not entity.judgement_issue:
            continue
        if not in_rule_scope(entity, definition):
            continue
        counts[entity.advertiser_id, entity.judgement_issue] += 1
    return [
        {"advertiser_id": adv, "reason": reason, "count": str(count)}
        for (adv, reason), count in sorted(counts.items())
    ]


def mark_missing_metrics(
    entities: dict[str, Entity],
    facts: dict[str, dict[str, dict[str, Decimal | None]]],
    definition: RuleDefinition,
) -> None:
    """只核对规则实际用到的指标；缺少报表行的真实零投放仍按原有零值处理。"""
    ratios = {key: (num, den) for key, num, den, _scale in DERIVED_RATIOS}
    for key, entity in entities.items():
        if entity.kind != "material" or entity.judgement_issue:
            continue
        for condition in definition.conditions:
            row = facts.get(str(condition.range), {}).get(key)
            required = ratios.get(condition.metric, (condition.metric,))
            if row is not None and any(row.get(metric) is None for metric in required):
                entities[key] = replace(
                    entity, judgement_issue="MATERIAL_METRIC_MISSING"
                )
                break


def unlinked_report_accounts(
    values: dict[tuple[str, str], bool],
    linked_posts: set[tuple[str, str]],
) -> set[str]:
    """共用窗口金额判定：只有有价值且无广告归属的帖子才使账户缺数。"""
    return {
        adv
        for (adv, mid), valuable in values.items()
        if valuable and (adv, mid) not in linked_posts
    }
