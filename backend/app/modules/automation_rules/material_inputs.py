"""广告内素材输入：创意事实按官方创意身份归到视频，缺口时停止自动判断。"""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from itertools import groupby
from uuid import UUID

from sqlmodel import Session, col, select

from app.modules.ads.creative_models import AdCreative
from app.modules.ads.directory import latest_ad_materials
from app.modules.ads.models import AdMaterialReference
from app.modules.automation_rules.creative_integrity import group_issues
from app.modules.automation_rules.inputs import (
    AccountInfo,
    Entity,
    in_rule_scope,
)
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.automation_rules.statuses import known_status
from app.modules.reporting.metric_catalog import DERIVED_RATIOS, STORED_KEYS


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
) -> dict[str, Entity]:
    if not accounts:
        return {}
    creatives = session.exec(
        select(AdCreative).where(
            AdCreative.tenant_id == tenant_id,
            col(AdCreative.advertiser_id).in_(list(accounts)),
        )
    ).all()
    by_video = defaultdict(list)
    conflicted_ads = set()
    for creative in creatives:
        if creative.identity_status == "RESOLVED" and creative.material_type == "VIDEO":
            by_video[
                creative.advertiser_id,
                creative.ad_remote_id,
                creative.platform_material_id,
            ].append(creative.creative_id)
        elif creative.identity_status == "CONFLICT":
            conflicted_ads.add((creative.advertiser_id, creative.ad_remote_id))
    issues = (
        group_issues(
            session,
            tenant_id=tenant_id,
            accounts=accounts,
            definition=definition,
            now=now,
        )
        if definition and now
        else {}
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
            ids = by_video[adv, ad_id, use.platform_material_id]
            issue = None
            group_id = (ad.parent_key or "").rsplit(":", 1)[-1]
            if not use.ad_material_id or use.operation_status is None:
                issue = "MATERIAL_IDENTITY_AMBIGUOUS"
            elif parents_enabled is None:
                issue = "MATERIAL_PARENT_UNKNOWN"
            elif (adv, group_id) in issues:
                issue = issues[adv, group_id]
            elif (adv, ad_id) in conflicted_ads:
                issue = "MATERIAL_CREATIVE_CONFLICT"
            elif not ids:
                issue = "MATERIAL_CREATIVE_MISSING"
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
                report_ids=tuple(sorted(set(ids))),
                judgement_issue=issue,
                parents_enabled=parents_enabled,
            )
    return entities


def material_facts(
    entities: dict[str, Entity],
    posts: dict[str, dict[str, Decimal | None]],
) -> dict[str, dict[str, Decimal | None]]:
    """每个创意只归到其所属广告的视频；未知或不完整数据不参与判断。"""
    result = {}
    for entity in entities.values():
        if entity.kind != "material" or entity.judgement_issue:
            continue
        rows = [
            posts[f"creative:{entity.advertiser_id}:{pid}"]
            for pid in entity.report_ids
            if f"creative:{entity.advertiser_id}:{pid}" in posts
        ]
        if rows:
            # 任一创意缺指标时保留 NULL，不能用其他创意的金额掩盖缺数。
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
