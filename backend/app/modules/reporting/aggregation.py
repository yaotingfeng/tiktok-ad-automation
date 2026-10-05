"""本地六维报表：授权范围、非重叠时间桶与指标覆盖共同决定可相加事实。"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.models import AdMaterialReference, AdObject, CampaignNameProjection
from app.modules.reporting.contracts import CORE_METRICS, supports_metric
from app.modules.reporting.filters import apply_authorized_scope, compile_filter
from app.modules.reporting.models import ReportCoverage, ReportFact
from app.modules.reporting.schemas import (
    Availability,
    MetricVector,
    ReportingFilter,
    ReportRow,
)

D0 = "native_growth_ad_revenue_value_d0"
TOTAL_REVENUE = "native_growth_total_ad_impression_value"
IMPRESSION_EVENTS = "native_growth_total_ad_impression_event_count"
DERIVED_METRICS = frozenset(
    {"d0_roas", "ad_revenue_roas", "cost_per_ad_impression_event", "ctr"}
)
CONTRACTS = {
    "account": ("basic_account",), "campaign": ("basic_campaign",),
    "drama": ("basic_campaign",), "adgroup": ("basic_adgroup",),
    "ad": ("basic_ad", "basic_smart_plus_ad"),
    "material": ("material_overview", "material_breakdown"),
}


def scoped_rows(session: Session, model: Any, *, context: TenantContext, bc_id: str,
                filters: ReportingFilter) -> list[Any]:
    """每次读取显式传入行的 tenant 列，并使用 B1 的当前 BC grant 子查询。"""
    statement = apply_authorized_scope(
        session, select(model), context=context, bc_id=bc_id,
        tenant_column=cast(Any, col(model.tenant_id)), advertiser_column=cast(Any, col(model.advertiser_id)),
    )
    if filters.advertiser_ids:
        statement = statement.where(col(model.advertiser_id).in_(filters.advertiser_ids))
    if model in {ReportFact, ReportCoverage}:
        # A first page must not materialize every historical fact for the BC.
        # The selected date is local to each account, so use a conservative UTC
        # buffer here and let ``select_facts`` apply the exact account timezone
        # boundary after rows are loaded.  The report-period index covers this
        # predicate and keeps the small staging host below its memory budget.
        contracts = CONTRACTS[filters.dimension]
        start = datetime.combine(filters.start_date - timedelta(days=1), time.min, UTC)
        end = datetime.combine(filters.end_date + timedelta(days=2), time.min, UTC)
        statement = statement.where(
            col(model.report_contract).in_(contracts),
            col(model.bucket_start) < end,
            col(model.bucket_end) > start,
        )
    return list(session.exec(statement).all())


def period(filters: ReportingFilter, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    return (datetime.combine(filters.start_date, time.min, zone).astimezone(UTC),
            datetime.combine(filters.end_date + timedelta(days=1), time.min, zone).astimezone(UTC))


def bucket_key(vector: MetricVector) -> tuple[Any, ...]:
    return (vector.currency, vector.timezone, vector.attribution,
            tuple(sorted((name, state) for name, state in vector.availability.items() if name not in DERIVED_METRICS)))


def aggregate_metrics(rows: Sequence[MetricVector]) -> MetricVector:
    """仅同口径/覆盖相加；ROAS 为收入总和/消耗总和，零分母仍为空。"""
    if not rows:
        raise ValueError("cannot aggregate empty metric rows")
    if any(bucket_key(row) != bucket_key(rows[0]) for row in rows[1:]):
        raise ValueError("incompatible coordinates or availability buckets")
    values: dict[str, Decimal | None] = {}
    states = {
        name: state
        for name, state in rows[0].availability.items()
        if name not in DERIVED_METRICS
    }
    for name, state in states.items():
        amounts = [row.values.get(name) for row in rows]
        if state == "AVAILABLE" and all(value is not None for value in amounts):
            if any(not cast(Decimal, value).is_finite() for value in amounts):
                raise ValueError("metrics require finite decimals")
            values[name] = sum((cast(Decimal, value) for value in amounts), Decimal(0))
        else:
            values[name] = None
            if state == "AVAILABLE":
                states[name] = "MISSING"
    for derived, numerator, denominator in (
        ("d0_roas", D0, "spend"),
        ("ad_revenue_roas", TOTAL_REVENUE, "spend"),
        ("cost_per_ad_impression_event", "spend", IMPRESSION_EVENTS),
        ("ctr", "clicks", "impressions"),
    ):
        numerator_value = values.get(numerator)
        denominator_value = values.get(denominator)
        values[derived] = (
            numerator_value / denominator_value
            if numerator_value is not None and denominator_value
            else None
        )
        if values[derived] is not None:
            states[derived] = "AVAILABLE"
        else:
            unavailable = {states.get(numerator), states.get(denominator)}
            states[derived] = next(
                (
                    state
                    for state in ("FAILED", "UNSUPPORTED", "UNAVAILABLE", "MISSING")
                    if state in unavailable
                ),
                "MISSING",
            )
    return MetricVector(currency=rows[0].currency, timezone=rows[0].timezone,
                        attribution=rows[0].attribution, values=values, availability=states)


def _coverage_matches(coverage: ReportCoverage, fact: ReportFact) -> bool:
    subject_id = fact.subject_key[2] if fact.subject_key[0] == "material" else fact.subject_key[1]
    return (coverage.advertiser_id == fact.advertiser_id
            and coverage.report_contract == fact.report_contract
            and coverage.currency == fact.currency and coverage.timezone == fact.timezone
            and coverage.attribution == fact.attribution
            and coverage.bucket_start <= fact.bucket_start and coverage.bucket_end >= fact.bucket_end
            and (not coverage.filter_ids or subject_id in coverage.filter_ids)
            and fact.metric_name in coverage.requested_metrics)


def select_facts(facts: Sequence[ReportFact], coverages: Sequence[ReportCoverage],
                 filters: ReportingFilter, *, grain: str | None = None) -> tuple[ReportFact, ...]:
    """日期须完整包含桶，绝不按比例拆 RANGE。每个 subject 选择一种非重叠粒度。

    DAY 优先，随后完整 RANGE，再 HOUR；同粒度重叠时新发布覆盖旧发布。
    新 COMPLETE_EMPTY 覆盖同一目标/指标时屏蔽旧事实，缺页/失败保留旧值但覆盖标缺。
    """
    candidates: dict[tuple[Any, ...], list[ReportFact]] = defaultdict(list)
    for fact in facts:
        if fact.report_contract not in CONTRACTS[filters.dimension]:
            continue
        if fact.subject_key[0] == "material" and len(fact.subject_key) != 5:
            raise ValueError("material subject requires five typed parts")
        start, end = period(filters, fact.timezone)
        if not start <= fact.bucket_start < fact.bucket_end <= end:
            continue
        if grain and fact.granularity != grain:
            continue
        empty = any(c.status == "COMPLETE_EMPTY" and c.request_sequence >= fact.request_sequence
                    and _coverage_matches(c, fact) for c in coverages)
        if empty:
            continue
        key = (fact.advertiser_id, tuple(fact.subject_key), fact.report_contract,
               fact.currency, fact.timezone, fact.attribution)
        candidates[key].append(fact)
    selected: list[ReportFact] = []
    for rows in candidates.values():
        selected_grain = grain or min((r.granularity for r in rows), key={"DAY": 0, "RANGE": 1, "HOUR": 2}.__getitem__)
        # 整个 subject 使用同一粒度，防止 spend 是 DAY、收入却是 HOUR 的伪 ROAS。
        by_bucket: dict[tuple[datetime, datetime], list[ReportFact]] = defaultdict(list)
        for row in rows:
            if row.granularity == selected_grain:
                by_bucket[(row.bucket_start, row.bucket_end)].append(row)
        occupied: list[tuple[datetime, datetime]] = []
        for bounds, items in sorted(by_bucket.items(), key=lambda item: (-max(r.request_sequence for r in item[1]), item[0])):
            if any(bounds[0] < end and start < bounds[1] for start, end in occupied):
                continue
            occupied.append(bounds)
            latest: dict[str, ReportFact] = {}
            for item in items:
                if item.metric_name not in latest or item.request_sequence > latest[item.metric_name].request_sequence:
                    latest[item.metric_name] = item
            selected.extend(latest.values())
    return tuple(selected)


def _aggregate_fact_vectors(facts: Sequence[ReportFact]) -> tuple[MetricVector, ...]:
    # 先重组每个 subject/日期的完整指标向量，缺失收入不能借另一 subject 的收入补齐。
    subjects: dict[tuple[Any, ...], list[ReportFact]] = defaultdict(list)
    for fact in facts:
        subjects[(fact.advertiser_id, tuple(fact.subject_key), fact.bucket_start, fact.bucket_end,
                  fact.report_contract, fact.currency, fact.timezone, fact.attribution)].append(fact)
    buckets: dict[tuple[Any, ...], list[MetricVector]] = defaultdict(list)
    for items in subjects.values():
        first = items[0]
        values = dict.fromkeys(CORE_METRICS)
        states: dict[str, Availability] = {name: "MISSING" if supports_metric(report_contract=first.report_contract, metric_name=name)
                                          else "UNSUPPORTED" for name in CORE_METRICS}
        for item in items:
            if item.metric_name not in values:
                raise ValueError("unknown canonical metric")
            state = cast(Availability, item.availability)
            if state == "AVAILABLE" and item.value is not None:
                values[item.metric_name] = (values[item.metric_name] or Decimal(0)) + item.value
                states[item.metric_name] = "AVAILABLE"
            else:
                values[item.metric_name] = None
                states[item.metric_name] = state if state != "AVAILABLE" else "MISSING"
        vector = MetricVector(currency=first.currency, timezone=first.timezone, attribution=first.attribution,
                              values=values, availability=states)
        buckets[bucket_key(vector)].append(vector)
    return tuple(aggregate_metrics(rows) for _, rows in sorted(buckets.items()))


def latest_projections(rows: Sequence[CampaignNameProjection]) -> dict[tuple[str, str], CampaignNameProjection]:
    result: dict[tuple[str, str], CampaignNameProjection] = {}
    for row in rows:
        key = (row.advertiser_id, row.campaign_remote_id)
        if key not in result or (
            row.grouping_revision,
            row.name_revision,
            row.observed_at,
        ) > (
            result[key].grouping_revision,
            result[key].name_revision,
            result[key].observed_at,
        ):
            result[key] = row
    return result


def drama_key(bc_id: str, advertiser_id: str, campaign_id: str,
              projection: CampaignNameProjection | None) -> str:
    if projection and projection.status == "VALID":
        # JSON 元组编码避免名称内 ':' 产生碰撞；账户不参与有效剧的 BC 范围身份。
        return f"{bc_id}:drama:{projection.provider_label}:{projection.drama_name}"
    return f"{bc_id}:external:{advertiser_id}:{campaign_id}"


def _created(value: Any, timezone: str) -> date | None:
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            return datetime.fromtimestamp(float(value), ZoneInfo(timezone)).date()
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (parsed.replace(tzinfo=ZoneInfo(timezone)) if parsed.tzinfo is None else parsed.astimezone(ZoneInfo(timezone))).date()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _number(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _text_number(value: Any) -> str | None:
    number = _number(value)
    return format(number, "f") if number is not None else None


def _config_display(entity: AdObject | None) -> dict[str, str | None]:
    """把目录配置转成报表可读字段，避免把原始 JSON 直接暴露到表格。"""
    if entity is None:
        return {}
    config = entity.configuration or {}
    target = _text_number(config.get("roas_bid") or config.get("target_roas"))
    budget = _text_number(
        config.get("budget")
        or config.get("daily_budget")
        or config.get("campaign_daily_budget")
    )
    return {
        "target_roas": target,
        "budget": budget,
        "budget_mode": str(config.get("budget_mode")) if config.get("budget_mode") else None,
        "optimization_goal": (
            str(config.get("optimization_goal"))
            if config.get("optimization_goal")
            else None
        ),
    }


def _entity_name(
    objects: dict[tuple[str, str, str], AdObject],
    advertiser_id: str,
    kind: str,
    remote_id: str | None,
) -> str | None:
    if not remote_id:
        return None
    entity = objects.get((advertiser_id, kind, remote_id))
    return entity.name or remote_id if entity else remote_id


def _hierarchy_display(
    *,
    accounts: dict[str, AdvertiserAccount],
    objects: dict[tuple[str, str, str], AdObject],
    advertiser_id: str,
    kind: str,
    remote_id: str,
    entity: AdObject | None,
) -> dict[str, str | None]:
    """按当前维度只返回需要的固定层级列，父级缺失时保留 ID 线索。"""
    account = accounts.get(advertiser_id)
    display: dict[str, str | None] = {
        "account_id": advertiser_id,
        "account_name": account.name or advertiser_id if account else advertiser_id,
    }
    if kind == "account":
        return display
    campaign_id: str | None = remote_id if kind == "campaign" else None
    adgroup_id: str | None = remote_id if kind == "adgroup" else None
    current = entity
    if current is not None:
        if current.kind == "adgroup":
            campaign_id = current.parent_remote_id
        elif current.kind == "ad":
            adgroup_id = current.parent_remote_id
            parent = objects.get((advertiser_id, "adgroup", adgroup_id or ""))
            campaign_id = parent.parent_remote_id if parent else None
    if kind == "campaign":
        display["campaign_id"] = remote_id
        display["campaign_name"] = entity.name or remote_id if entity else remote_id
    elif kind == "adgroup":
        display.update(
            {
                "campaign_id": campaign_id,
                "campaign_name": _entity_name(objects, advertiser_id, "campaign", campaign_id),
                "adgroup_id": remote_id,
                "adgroup_name": entity.name or remote_id if entity else remote_id,
            }
        )
    elif kind == "ad":
        display.update(
            {
                "campaign_id": campaign_id,
                "campaign_name": _entity_name(objects, advertiser_id, "campaign", campaign_id),
                "adgroup_id": adgroup_id,
                "adgroup_name": _entity_name(objects, advertiser_id, "adgroup", adgroup_id),
                "ad_id": remote_id,
                "ad_name": entity.name or remote_id if entity else remote_id,
            }
        )
    config = _config_display(entity)
    if config.get("target_roas"):
        display["target_roas"] = config["target_roas"]
    if config.get("optimization_goal"):
        display["optimization_goal"] = config["optimization_goal"]
    if kind == "campaign" and config.get("budget"):
        display["campaign_budget"] = config["budget"]
    if kind == "adgroup" and config.get("budget"):
        display["adgroup_budget"] = config["budget"]
    return display


def _entity_matches(entity: AdObject | None, projection: CampaignNameProjection | None,
                    filters: ReportingFilter, timezone: str) -> bool:
    for allowed, value in ((filters.ad_types, entity.ad_type if entity else None),
                           (filters.operation_statuses, entity.operation_status if entity else None),
                           (filters.review_statuses, entity.review_status if entity else None)):
        if allowed and value not in allowed:
            return False
    if filters.naming_status and (projection.status if projection else "INVALID") != filters.naming_status:
        return False
    config = entity.configuration if entity else {}
    if filters.budget_modes and config.get("budget_mode") not in filters.budget_modes:
        return False
    created = _created(config.get("create_time"), timezone)
    if filters.created_from and (created is None or created < filters.created_from):
        return False
    if filters.created_to and (created is None or created > filters.created_to):
        return False
    target = _number(config.get("roas_bid"))
    for bound, lower in ((filters.min_target_roas, True), (filters.max_target_roas, False)):
        if bound is not None and (target is None or (target < bound if lower else target > bound)):
            return False
    return True


def _passes_filter(row: ReportRow, filters: ReportingFilter) -> bool:
    ids = {row.row_key, *(ref.remote_id for ref in row.refs)}
    ids.update(value for key, value in row.display.items() if key.endswith("_id") and value)
    if filters.ids and not ids.intersection(filters.ids):
        return False
    text = " ".join([row.row_key, *sorted(ids), *(value for value in row.display.values() if value)]).casefold()
    if filters.query and any(word.casefold() not in text for word in filters.query.split()):
        return False
    bounds = (("spend", filters.min_spend, filters.max_spend),
              ("d0_roas", filters.min_d0_roas, filters.max_d0_roas))
    if any(low is not None or high is not None for _, low, high in bounds):
        return any(all((low is None or (v.values.get(name) is not None and cast(Decimal, v.values[name]) >= low))
                       and (high is None or (v.values.get(name) is not None and cast(Decimal, v.values[name]) <= high))
                       for name, low, high in bounds) for v in row.metric_buckets)
    return True


def _sort_rows(rows: Sequence[ReportRow], filters: ReportingFilter) -> tuple[ReportRow, ...]:
    ordered = sorted(rows, key=lambda row: row.row_key)
    if filters.sort_by == "spend":
        # 混币种行没有可排序的合计金额，明确拒绝而非取 max 或偷偷换汇。
        if any(len(row.metric_buckets) > 1 for row in rows):
            raise ValueError("spend sorting requires one compatible metric bucket per row")
        currencies = {v.currency for row in rows for v in row.metric_buckets}
        if len(currencies) > 1:
            raise ValueError("spend sorting requires one currency")
        def spend_key(row: ReportRow) -> tuple[bool, Decimal]:
            value = row.metric_buckets[0].values.get("spend") if row.metric_buckets else None
            amount = cast(Decimal, value) if value is not None else Decimal(0)
            # Missing values remain at the end for both directions.
            return (value is None, -amount if filters.sort_direction == "desc" else amount)

        return tuple(sorted(ordered, key=spend_key))
    return tuple(sorted(ordered, key=lambda row: (row.display.get("name") or "") if filters.sort_by == "name" else row.row_key,
                        reverse=filters.sort_direction == "desc"))


def _row_coverage(items: Sequence[ReportFact], coverages: Sequence[ReportCoverage],
                  advertiser_id: str, remote_id: str, filters: ReportingFilter) -> dict[str, object]:
    rows = [c for c in coverages if c.advertiser_id == advertiser_id and c.report_contract in CONTRACTS[filters.dimension]
            and (not c.filter_ids or remote_id in c.filter_ids)
            and period(filters, c.timezone)[0] <= c.bucket_start < c.bucket_end <= period(filters, c.timezone)[1]]
    if not rows:
        return {"status": "MISSING", "reason": "coverage_unavailable"}
    latest: dict[tuple[Any, ...], ReportCoverage] = {}
    for row in rows:
        key = (row.bucket_start, row.bucket_end, row.report_contract, row.currency, row.timezone, tuple(sorted(row.requested_metrics)))
        if key not in latest or row.request_sequence > latest[key].request_sequence:
            latest[key] = row
    statuses = sorted({row.status for row in latest.values()})
    complete = all(state in {"COMPLETE", "COMPLETE_EMPTY"} for state in statuses)
    intervals = sorted({(row.bucket_start, row.bucket_end) for row in latest.values()})
    # 只有已证实连续覆盖整个所选日期窗口才标 COMPLETE。
    start, end = period(filters, rows[0].timezone)
    cursor = start
    for left, right in intervals:
        if left > cursor:
            complete = False
        cursor = max(cursor, right)
    complete = complete and cursor >= end
    status = ("COMPLETE_EMPTY" if not items and statuses == ["COMPLETE_EMPTY"] else "COMPLETE") if complete else "INCOMPLETE"
    if statuses == ["FAILED"]:
        status = "FAILED"
    return {"status": status, "source_statuses": statuses,
            "published_versions": sorted({row.published_version for row in latest.values()})}


def build_dimension_rows(session: Session, *, context: TenantContext, bc_id: str,
                         filters: ReportingFilter) -> tuple[ReportRow, ...]:
    compile_filter(filters)
    accounts = {row.advertiser_id: row for row in scoped_rows(session, AdvertiserAccount, context=context, bc_id=bc_id, filters=filters)}
    objects = {(row.advertiser_id, row.kind, row.remote_id): row for row in scoped_rows(session, AdObject, context=context, bc_id=bc_id, filters=filters)}
    projections = latest_projections(scoped_rows(session, CampaignNameProjection, context=context, bc_id=bc_id, filters=filters))
    coverages = scoped_rows(session, ReportCoverage, context=context, bc_id=bc_id, filters=filters)
    facts = select_facts(scoped_rows(session, ReportFact, context=context, bc_id=bc_id, filters=filters), coverages, filters)
    if filters.dimension == "material":
        return _build_material_rows(facts, scoped_rows(session, AdMaterialReference, context=context, bc_id=bc_id, filters=filters), filters,
                                    directory=objects, projections=projections)
    kind = "campaign" if filters.dimension == "drama" else filters.dimension
    identities = {(fact.advertiser_id, fact.subject_key[1]) for fact in facts if fact.subject_key[0] == kind}
    identities.update((adv, rid) for adv, obj_kind, rid in objects if obj_kind == kind)
    if kind == "account":
        identities.update((adv, adv) for adv in accounts)
    facts_by_identity: dict[tuple[str, str, str], list[ReportFact]] = defaultdict(list)
    for fact in facts:
        if len(fact.subject_key) >= 2:
            facts_by_identity[(fact.advertiser_id, fact.subject_key[0], fact.subject_key[1])].append(fact)
    grouped: dict[str, list[ReportRow]] = defaultdict(list)
    for adv, rid in sorted(identities):
        entity = objects.get((adv, kind, rid))
        projection = projections.get((adv, rid)) if kind == "campaign" else None
        if not _entity_matches(entity, projection, filters, accounts[adv].timezone):
            continue
        items = facts_by_identity.get((adv, kind, rid), [])
        key = drama_key(bc_id, adv, rid, projection) if filters.dimension == "drama" else f"{adv}:{kind}:{rid}"
        refs: tuple[EntityRef, ...] = () if kind == "account" else (EntityRef(context.tenant_id, adv, cast(Any, kind), rid),)
        name = (projection.drama_name if projection and projection.status == "VALID" else rid) if filters.dimension == "drama" else entity.name if entity else rid
        display = (
            {"name": name, "drama_name": name, "provider": projection.provider_label if projection else None}
            if filters.dimension == "drama"
            else _hierarchy_display(
                accounts=accounts,
                objects=objects,
                advertiser_id=adv,
                kind=kind,
                remote_id=rid,
                entity=entity,
            )
        )
        display.update({
            "name": name,
            "remote_id": rid,
            "provider": projection.provider_label if projection else None,
            "naming_status": projection.status if projection else "INVALID",
            "ad_type": entity.ad_type if entity else None,
            "operation_status": entity.operation_status if entity else None,
            "review_status": entity.review_status if entity else None,
        })
        row = ReportRow(row_key=key, refs=refs,
            display=display,
            metric_buckets=_aggregate_fact_vectors(items),
            coverage=_row_coverage(items, coverages, adv, rid, filters),
            directory_versions={f"{adv}:{rid}": entity.published_version} if entity else {})
        grouped[key].append(row)
    result: list[ReportRow] = []
    for key, rows in grouped.items():
        vectors: dict[tuple[Any, ...], list[MetricVector]] = defaultdict(list)
        for row in rows:
            for vector in row.metric_buckets:
                vectors[bucket_key(vector)].append(vector)
        refs = tuple(ref for row in rows for ref in row.refs)
        statuses = {row.coverage["status"] for row in rows}
        result_row = rows[0].model_copy(update={"refs": refs,
            "metric_buckets": tuple(aggregate_metrics(v) for _, v in sorted(vectors.items())),
            "coverage": {"status": next(iter(statuses)) if len(statuses) == 1 else "INCOMPLETE", "members": [row.coverage for row in rows]},
            "membership_digest": sha256(json.dumps(sorted((str(ref.tenant_id), ref.advertiser_id, ref.kind, ref.remote_id) for ref in refs)).encode()).hexdigest(),
            "directory_versions": {key: value for row in rows for key, value in row.directory_versions.items()}})
        if _passes_filter(result_row, filters):
            result.append(result_row)
    return _sort_rows(result, filters)


def _build_material_rows(facts: Sequence[ReportFact], materials: Sequence[AdMaterialReference], filters: ReportingFilter,
                         *, directory: dict[tuple[str, str, str], AdObject] | None = None,
                         projections: dict[tuple[str, str], CampaignNameProjection] | None = None) -> tuple[ReportRow, ...]:
    directory, projections = directory or {}, projections or {}
    material_index: dict[tuple[str, str, str, str], list[AdMaterialReference]] = defaultdict(list)
    for material in materials:
        # Material facts carry the platform main-material identity while the
        # directory may expose either the main or ad-level identity.  Index both
        # forms once; scanning all 48k references for every fact made the
        # material page quadratic and exhausted the staging host's memory.
        main_key = (
            (material.advertiser_id, material.ad_remote_id,
             material.main_material_id, material.main_material_type)
            if material.main_material_id and material.main_material_type
            else None
        )
        platform_key = (
            material.advertiser_id, material.ad_remote_id,
            material.platform_material_id, material.material_type
        )
        if main_key is not None:
            material_index[main_key].append(material)
        if platform_key != main_key:
            material_index[platform_key].append(material)
    grouped: dict[tuple[str, ...], list[ReportFact]] = defaultdict(list)
    for fact in facts:
        if len(fact.subject_key) != 5 or fact.subject_key[0] != "material":
            raise ValueError("material subject requires five typed parts")
        grouped[(fact.advertiser_id, *fact.subject_key[1:])].append(fact)
    result = []
    for (adv, dimension, grouping, main_id, main_type), items in grouped.items():
        # Every metric fact must carry exactly one typed ad identity. A single
        # mapped row cannot prove another row's parent spend belongs to it.
        candidates_by_item: list[AdMaterialReference] = []
        proof_complete = True
        for item in items:
            identity_fields = [
                (field, str(item.attributes[field]))
                for field in ("ad_id", "ad_id_v2", "smart_plus_ad_id")
                if item.attributes.get(field)
            ]
            if len(identity_fields) != 1:
                proof_complete = False
                continue
            _identity_type, ad_id = identity_fields[0]
            ad_material_id = item.attributes.get("ad_material_id")
            if "ad_material_id" in item.attributes and not isinstance(ad_material_id, str):
                proof_complete = False
                continue
            matches = [
                material
                for material in material_index.get((adv, ad_id, main_id, main_type), ())
                if material.complete
                and ("ad_material_id" not in item.attributes or material.ad_material_id == ad_material_id)
            ]
            if len(matches) != 1:
                proof_complete = False
                continue
            candidates_by_item.append(matches[0])
        candidates_by_id = {
            (candidate.ad_remote_id, candidate.platform_material_id,
             candidate.ad_material_id, candidate.material_type): candidate
            for candidate in candidates_by_item
        }
        proven = proof_complete and len(candidates_by_item) == len(items) and bool(candidates_by_id)
        ad_ids = {candidate.ad_remote_id for candidate in candidates_by_item}
        entity = directory.get((adv, "ad", next(iter(ad_ids), grouping))) if proven else None
        if not _entity_matches(entity, None, filters, items[0].timezone):
            continue
        vectors = _aggregate_fact_vectors(items)
        if not proven:
            # Keep the provider's numeric facts visible, but make their
            # unproven attribution explicit on every metric and expose no use_ref.
            vectors = tuple(v.model_copy(update={
                "availability": dict.fromkeys(v.availability, "UNSUPPORTED")
            }) for v in vectors)
        status = "COMPLETE" if proven else ("INCOMPLETE" if candidates_by_item else "UNSUPPORTED")
        material_name = next(iter(candidates_by_id.values())).name if proven else main_id
        row = ReportRow(row_key=json.dumps([adv, "material", dimension, grouping, main_id, main_type]),
                        display={"name": material_name, "material_name": material_name, "main_material_id": main_id,
                                 "grouping_id": grouping, "material_type": main_type},
                        refs=tuple(item.use_ref.ad_ref for item in candidates_by_id.values()) if proven else (),
                        material_uses=tuple(item.use_ref for item in candidates_by_id.values()) if proven else (),
                        metric_buckets=vectors, coverage={"status": status,
                                                        "reason": "material_coverage_verified" if proven else "ad_usage_unproven"})
        if _passes_filter(row, filters):
            result.append(row)
    return _sort_rows(result, filters)
