"""广告内素材输入：报表帖子归到广告内视频，执行只引用当前唯一使用身份。"""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.modules.ads.directory import latest_ad_materials
from app.modules.ads.models import AdMaterialReference
from app.modules.automation_rules.inputs import AccountInfo, Entity, local_window
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.reporting.metric_catalog import DERIVED_RATIOS, STORED_KEYS
from app.modules.reporting.store_models import MaterialPostLink


def material_use_id(ad_id: str, video_id: str, material_type: str) -> str:
    """广告内视频的稳定身份；广告内素材 ID 重建不改变规则频控/选择身份。"""
    raw = json.dumps([ad_id, video_id, material_type], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


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
    rows = session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == tenant_id,
            col(AdMaterialReference.advertiser_id).in_(list(accounts)),
            col(AdMaterialReference.complete).is_(True),
        )
    ).all()
    links = session.exec(
        select(MaterialPostLink).where(
            MaterialPostLink.tenant_id == tenant_id,
            col(MaterialPostLink.advertiser_id).in_(list(accounts)),
        )
    ).all()
    by_ad = defaultdict(list)
    by_video = defaultdict(list)
    owners = defaultdict(set)
    unresolved_ads = set()
    for link in links:
        owners[link.advertiser_id, link.main_material_id].add(
            (link.ad_remote_id, link.platform_material_id)
        )
        if link.platform_material_id is None:
            unresolved_ads.add((link.advertiser_id, link.ad_remote_id))
        else:
            by_video[
                link.advertiser_id, link.ad_remote_id, link.platform_material_id
            ].append(link)
    for row in rows:
        by_ad[row.advertiser_id, row.ad_remote_id].append(row)
    unlinked_accounts = unlinked_report_accounts(
        session, tenant_id, accounts, definition, now
    )
    entities = {}
    for (adv, ad_id), candidates in sorted(by_ad.items()):
        ad = ads.get(f"ad:{adv}:{ad_id}")
        if ad is None:
            continue
        info = accounts[adv]
        for use in latest_ad_materials(candidates):
            refs = by_video[adv, ad_id, use.platform_material_id]
            issue = None
            if not use.ad_material_id or use.operation_status is None:
                issue = "MATERIAL_IDENTITY_AMBIGUOUS"
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
                attributes={"name": use.name or use.platform_material_id},
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
        scope = definition.apply_to
        if scope.status == "SELECTED":
            if f"{entity.advertiser_id}:{entity.remote_id}" not in scope.object_ids:
                continue
        elif (
            entity.status is not None
            and entity.status != {"ACTIVE": "ENABLE", "PAUSED": "DISABLE"}[scope.status]
        ):
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
    session: Session,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    definition: RuleDefinition | None,
    now: datetime | None,
) -> set[str]:
    """选中日期内任何完全未定位的帖子都可能属于当前素材，不能静默少算历史回收。"""
    if definition is None or now is None:
        return set()
    ranges = {str(c.range) for c in definition.conditions if c.range}
    if not ranges:
        return set()
    windows = [
        [local_window(r, info.timezone, now) for r in ranges]
        for info in accounts.values()
    ]
    rows = session.connection().execute(
        text("""
        SELECT DISTINCT f.advertiser_id FROM material_report_day f
        JOIN unnest(CAST(:advs AS text[]), CAST(:starts AS date[]), CAST(:ends AS date[]))
            AS w(adv,s,e) ON w.adv=f.advertiser_id AND f.stat_date BETWEEN w.s AND w.e
        WHERE f.tenant_id=:t AND NOT EXISTS (
            SELECT 1 FROM material_post_link l WHERE l.tenant_id=f.tenant_id
                AND l.advertiser_id=f.advertiser_id AND l.main_material_id=f.main_material_id)
    """),
        {
            "t": tenant_id,
            "advs": list(accounts),
            "starts": [min(w[0] for w in ws) for ws in windows],
            "ends": [max(w[1] for w in ws) for ws in windows],
        },
    )
    return {row.advertiser_id for row in rows}
