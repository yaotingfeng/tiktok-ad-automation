"""B2 报表聚合。

本模块只消费已经发布的 ``ReportFact`` 和目录身份。事实查询先重建租户权限，
再用所选 BC 的授权账户谓词收窄；BC 不存在于事实主键中，不能从 source run
或目录行反推。素材报表没有广告级使用身份时始终返回 UNSUPPORTED，避免把
父广告金额复制给多个 VID。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.modules.ads.models import AdMaterialReference, AdObject, CampaignNameProjection
from app.modules.reporting.filters import authorized_grants, compile_filter
from app.modules.reporting.models import ReportFact
from app.modules.reporting.schemas import (
    Availability,
    MetricVector,
    ReportingFilter,
    ReportRow,
)
from app.modules.tenants.permissions import require_tenant

CORE_METRICS = (
    "spend",
    "native_growth_ad_revenue_value_d0",
    "native_growth_total_ad_impression_value",
    "impressions",
    "clicks",
)

_STATUS_RANK = {
    "MISSING": 1,
    "UNAVAILABLE": 2,
    "UNSUPPORTED": 3,
    "FAILED": 4,
}
_DIMENSION_CONTRACTS = {
    "account": ("account", "basic_account"),
    "campaign": ("campaign", "basic_campaign"),
    "adgroup": ("adgroup", "basic_adgroup"),
    "ad": ("ad", "basic_ad", "basic_smart_plus_ad"),
    "material": ("material", "material_overview", "material_breakdown"),
}


def _status(statuses: Sequence[str]) -> Availability:
    if not statuses:
        return "MISSING"
    if all(item == "AVAILABLE" for item in statuses):
        return "AVAILABLE"
    return cast(Availability, max(statuses, key=lambda item: _STATUS_RANK.get(item, 0)))


def _safe_decimal(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value)


def aggregate_metrics(rows: Sequence[MetricVector]) -> MetricVector:
    """在完全相同口径和可用性桶内求和，并从汇总值计算 D0 ROAS。

    缺失/失败行不会被当成零。调用方若需要同时展示完整与缺失覆盖，应按
    availability signature 分别调用本函数；混合口径直接拒绝。
    """

    if not rows:
        raise ValueError("cannot aggregate empty metric rows")
    first = rows[0]
    first_signature = tuple(sorted(first.availability.items()))
    for row in rows[1:]:
        if (
            row.currency != first.currency
            or row.timezone != first.timezone
            or row.attribution != first.attribution
            or row.optimization_goal != first.optimization_goal
        ):
            raise ValueError("metric rows have incompatible coordinates")
        if tuple(sorted(row.availability.items())) != first_signature:
            raise ValueError("metric rows have incompatible availability buckets")

    names = set().union(*(row.values for row in rows))
    values: dict[str, Decimal | None] = {}
    availability: dict[str, Availability] = {}
    for name in sorted(names):
        amounts: list[Decimal] = []
        states: list[str] = []
        for row in rows:
            if name not in row.values:
                states.append("MISSING")
                continue
            state = row.availability.get(name, "MISSING")
            amount = row.values.get(name)
            if state == "AVAILABLE" and amount is None:
                # MetricVector accepts the shape for transport convenience, but
                # an AVAILABLE metric without a value is not evidence of zero.
                states.append("MISSING")
            else:
                states.append(state)
            if state == "AVAILABLE" and amount is not None:
                amounts.append(_safe_decimal(amount) or Decimal("0"))
        state = _status(states)
        availability[name] = state
        values[name] = sum(amounts, Decimal("0")) if state == "AVAILABLE" else None

    # ROAS is a ratio of totals, never an average of row ratios.
    if "spend" in values and "native_growth_ad_revenue_value_d0" in values:
        spend_state = availability["spend"]
        revenue_state = availability["native_growth_ad_revenue_value_d0"]
        if spend_state == revenue_state == "AVAILABLE":
            spend = values["spend"]
            revenue = values["native_growth_ad_revenue_value_d0"]
            values["d0_roas"] = None if not spend else revenue / spend  # type: ignore[operator]
            availability["d0_roas"] = "AVAILABLE"
        else:
            values["d0_roas"] = None
            availability["d0_roas"] = _status((spend_state, revenue_state))

    return MetricVector(
        currency=first.currency,
        timezone=first.timezone,
        attribution=first.attribution,
        optimization_goal=first.optimization_goal,
        values=values,
        availability=availability,
    )


def _period(filters: ReportingFilter) -> tuple[datetime, datetime]:
    # ReportFact 时间都带时区；UTC 半开区间避免日期末尾精度问题。
    start = datetime.combine(filters.start_date, time.min, tzinfo=UTC)
    end = datetime.combine(filters.end_date + timedelta(days=1), time.min, tzinfo=UTC)
    return start, end


def _fact_rows(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
) -> tuple[ReportFact, ...]:
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read")
    grants = authorized_grants(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_ids=filters.advertiser_ids,
    )
    advertiser_ids = tuple(sorted({grant.advertiser_id for grant in grants}))
    if not advertiser_ids:
        return ()
    start, end = _period(filters)
    statement = select(ReportFact).where(
        ReportFact.tenant_id == context.tenant_id,
        col(ReportFact.advertiser_id).in_(advertiser_ids),
        ReportFact.bucket_start < end,
        ReportFact.bucket_end > start,
    )
    return tuple(session.exec(statement).all())


def _vector_from_fact(fact: ReportFact) -> MetricVector:
    state = fact.availability
    if state not in {"AVAILABLE", "MISSING", "UNAVAILABLE", "UNSUPPORTED", "FAILED"}:
        state = "FAILED"
    typed_state = cast(Availability, state)
    return MetricVector(
        currency=fact.currency,
        timezone=fact.timezone,
        attribution=fact.attribution,
        values={fact.metric_name: _safe_decimal(fact.value) if typed_state == "AVAILABLE" else None},
        availability={fact.metric_name: typed_state},
    )


def _row_key(advertiser_id: str, kind: str, remote_id: str) -> str:
    return f"{advertiser_id}:{kind}:{remote_id}"


def _latest_projections(
    session: Session, *, tenant_id: Any, advertiser_ids: Sequence[str]
) -> dict[tuple[str, str], CampaignNameProjection]:
    if not advertiser_ids:
        return {}
    rows = session.exec(
        select(CampaignNameProjection).where(
            CampaignNameProjection.tenant_id == tenant_id,
            col(CampaignNameProjection.advertiser_id).in_(advertiser_ids),
        )
    ).all()
    result: dict[tuple[str, str], CampaignNameProjection] = {}
    for row in rows:
        key = (row.advertiser_id, row.campaign_remote_id)
        if key not in result or row.name_revision > result[key].name_revision:
            result[key] = row
    return result


def _directory(
    session: Session,
    *,
    tenant_id: Any,
    advertiser_ids: Sequence[str],
) -> tuple[dict[tuple[str, str, str], AdObject], tuple[AdMaterialReference, ...]]:
    objects = session.exec(
        select(AdObject).where(
            AdObject.tenant_id == tenant_id,
            col(AdObject.advertiser_id).in_(advertiser_ids),
        )
    ).all()
    by_key = {(row.advertiser_id, row.kind, row.remote_id): row for row in objects}
    materials = tuple(
        session.exec(
            select(AdMaterialReference).where(
                AdMaterialReference.tenant_id == tenant_id,
                col(AdMaterialReference.advertiser_id).in_(advertiser_ids),
            )
        ).all()
    )
    return by_key, materials


def _material_identity(fact: ReportFact) -> tuple[str, str] | None:
    subject = fact.subject_key
    if not isinstance(subject, list) or len(subject) < 4 or subject[0] != "material":
        return None
    # A1's five-part subject and the pre-A1 four-part fixture are both read
    # defensively; neither shape proves an ad-level usage on its own.
    return (str(subject[-2]), str(subject[-1]))


def _unsupported_vector(*, currency: str, timezone: str, attribution: str) -> MetricVector:
    return MetricVector(
        currency=currency,
        timezone=timezone,
        attribution=attribution,
        values=dict.fromkeys(CORE_METRICS),
        availability=dict.fromkeys(CORE_METRICS, "UNSUPPORTED"),
    )


def _passes_filter(row: ReportRow, filters: ReportingFilter) -> bool:
    if filters.ids and row.row_key.split(":")[-1] not in filters.ids and row.row_key not in filters.ids:
        return False
    if filters.query:
        haystack = " ".join(value or "" for value in row.display.values()).casefold()
        if any(word.casefold() not in haystack for word in filters.query.split()):
            return False
    if filters.operation_statuses:
        status = row.display.get("operation_status")
        if status not in filters.operation_statuses:
            return False
    if filters.review_statuses:
        status = row.display.get("review_status")
        if status not in filters.review_statuses:
            return False
    for vector in row.metric_buckets:
        spend = vector.values.get("spend")
        if filters.min_spend is not None and (spend is None or spend < filters.min_spend):
            continue
        if filters.max_spend is not None and (spend is None or spend > filters.max_spend):
            continue
        return True
    return not (filters.min_spend is not None or filters.max_spend is not None)


def _aggregate_fact_vectors(facts: Sequence[ReportFact]) -> tuple[MetricVector, ...]:
    groups: dict[tuple[str, str, str], list[MetricVector]] = defaultdict(list)
    for fact in facts:
        groups[(fact.currency, fact.timezone, fact.attribution)].append(_vector_from_fact(fact))
    result: list[MetricVector] = []
    for _key, vectors in sorted(groups.items()):
        # ReportFact stores one metric per row. Reassemble the metric vector
        # first, otherwise spend and native D0 revenue would look like two
        # incompatible buckets and ROAS could never be derived.
        values: dict[str, Decimal | None] = {}
        availability: dict[str, Availability] = {}
        by_metric: dict[str, list[MetricVector]] = defaultdict(list)
        for vector in vectors:
            for name in vector.values:
                by_metric[name].append(vector)
        for name, metric_rows in by_metric.items():
            states = [row.availability[name] for row in metric_rows]
            state = _status(states)
            availability[name] = state
            amounts = [
                cast(Decimal, row.values[name])
                for row in metric_rows
                if row.availability[name] == "AVAILABLE" and row.values[name] is not None
            ]
            values[name] = sum(amounts, Decimal("0")) if state == "AVAILABLE" else None
        if values:
            result.append(
                aggregate_metrics(
                    [
                        MetricVector(
                            currency=_key[0],
                            timezone=_key[1],
                            attribution=_key[2],
                            values=values,
                            availability=availability,
                        )
                    ]
                )
            )
    return tuple(result)


def build_dimension_rows(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
) -> tuple[ReportRow, ...]:
    """构造六维行；目录缺失的报表身份仍保留，授权范围始终由 BC grant 决定。"""

    compile_filter(filters)
    facts = _fact_rows(session, context=context, bc_id=bc_id, filters=filters)
    grants = authorized_grants(session, context=context, bc_id=bc_id, advertiser_ids=filters.advertiser_ids)
    advertiser_ids = tuple(sorted({grant.advertiser_id for grant in grants}))
    directory, materials = _directory(
        session, tenant_id=context.tenant_id, advertiser_ids=advertiser_ids
    )
    projections = _latest_projections(
        session, tenant_id=context.tenant_id, advertiser_ids=advertiser_ids
    )
    dimension = filters.dimension
    if dimension == "drama":
        return _build_drama_rows(facts, directory, projections, filters)
    if dimension == "material":
        return _build_material_rows(facts, materials, filters)
    expected = _DIMENSION_CONTRACTS[dimension][0]
    contracts = set(_DIMENSION_CONTRACTS[dimension][1:])
    grouped: dict[str, list[ReportFact]] = defaultdict(list)
    for fact in facts:
        if fact.report_contract not in contracts or not fact.subject_key:
            continue
        if fact.subject_key[0] != expected or len(fact.subject_key) < 2:
            continue
        grouped[_row_key(fact.advertiser_id, expected, fact.subject_key[1])].append(fact)
    rows: list[ReportRow] = []
    for key, items in grouped.items():
        advertiser_id, kind, remote_id = key.split(":", 2)
        entity = directory.get((advertiser_id, kind, remote_id))
        display = {
            "name": entity.name if entity else None,
            "operation_status": entity.operation_status if entity else None,
            "review_status": entity.review_status if entity else None,
            "ad_type": entity.ad_type if entity else None,
        }
        refs = (entity.ref,) if entity else ()
        row = ReportRow(
            row_key=key,
            display=display,
            refs=refs,
            metric_buckets=_aggregate_fact_vectors(items),
            directory_versions={"published_version": entity.published_version} if entity else {},
        )
        if _passes_filter(row, filters):
            rows.append(row)
    return tuple(sorted(rows, key=lambda item: item.row_key))


def _build_drama_rows(
    facts: Sequence[ReportFact],
    directory: dict[tuple[str, str, str], AdObject],
    projections: dict[tuple[str, str], CampaignNameProjection],
    filters: ReportingFilter,
) -> tuple[ReportRow, ...]:
    grouped: dict[str, list[ReportFact]] = defaultdict(list)
    displays: dict[str, dict[str, str | None]] = {}
    refs: dict[str, list[EntityRef]] = defaultdict(list)
    for fact in facts:
        if fact.report_contract != "basic_campaign" or len(fact.subject_key) < 2:
            continue
        campaign_id = fact.subject_key[1]
        projection = projections.get((fact.advertiser_id, campaign_id))
        if projection and projection.status == "VALID":
            key = f"{fact.advertiser_id}:drama:{projection.provider_label}:{projection.drama_name}"
            display = {"provider": projection.provider_label, "drama_name": projection.drama_name}
        else:
            # Invalid/missing naming is an externally visible campaign, never
            # silently dropped from the drama view.
            key = f"{fact.advertiser_id}:external:{campaign_id}"
            display = {"provider": None, "drama_name": None, "external_campaign_id": campaign_id}
        grouped[key].append(fact)
        displays[key] = display
        entity = directory.get((fact.advertiser_id, "campaign", campaign_id))
        if entity and entity.ref not in refs[key]:
            refs[key].append(entity.ref)
    rows = []
    for key, items in grouped.items():
        row = ReportRow(
            row_key=key,
            display=displays[key],
            refs=tuple(refs[key]),
            metric_buckets=_aggregate_fact_vectors(items),
        )
        if _passes_filter(row, filters):
            rows.append(row)
    return tuple(sorted(rows, key=lambda item: item.row_key))


def _build_material_rows(
    facts: Sequence[ReportFact], materials: Sequence[AdMaterialReference], filters: ReportingFilter
) -> tuple[ReportRow, ...]:
    by_identity: dict[tuple[str, str, str], list[ReportFact]] = defaultdict(list)
    uses: dict[tuple[str, str, str], list[AdMaterialReference]] = defaultdict(list)
    for material in materials:
        uses[(material.advertiser_id, material.platform_material_id, material.material_type)].append(material)
    for fact in facts:
        if fact.report_contract not in {"material_overview", "material_breakdown"}:
            continue
        identity = _material_identity(fact)
        if identity is None:
            continue
        by_identity[(fact.advertiser_id, identity[0], identity[1])].append(fact)
    rows: list[ReportRow] = []
    for key, items in by_identity.items():
        advertiser_id, platform_id, material_type = key
        candidates = uses.get(key, [])
        proven_items: list[ReportFact] = []
        proven_refs: list[AdMaterialReference] = []
        for fact in items:
            attrs = fact.attributes or {}
            ad_id = attrs.get("ad_id")
            ad_material_id = attrs.get("ad_material_id")
            matches = candidates
            if isinstance(ad_id, str):
                matches = [item for item in matches if item.ad_remote_id == ad_id]
            if isinstance(ad_material_id, str):
                matches = [item for item in matches if item.ad_material_id == ad_material_id]
            if len(matches) == 1:
                proven_items.append(fact)
                if matches[0] not in proven_refs:
                    proven_refs.append(matches[0])
        # A fact without an unambiguous ad-level use is not additive. Keep an
        # explicit unsupported row so callers can explain why spend is absent.
        proven = len(proven_items) == len(items) and bool(proven_items)
        vectors: tuple[MetricVector, ...]
        if not proven:
            vectors = (
                _unsupported_vector(
                    currency=items[0].currency,
                    timezone=items[0].timezone,
                    attribution=items[0].attribution,
                ),
            )
            row_uses: tuple[MaterialUseRef, ...] = tuple(item.use_ref for item in candidates)
            row_refs = tuple(item.use_ref.ad_ref for item in candidates)
        else:
            vectors = _aggregate_fact_vectors(proven_items)
            row_uses = tuple(item.use_ref for item in proven_refs)
            row_refs = tuple(item.use_ref.ad_ref for item in proven_refs)
        row = ReportRow(
            row_key=f"{advertiser_id}:material:{platform_id}:{material_type}",
            display={"name": candidates[0].name if candidates else None, "material_type": material_type},
            refs=row_refs,
            material_uses=row_uses,
            metric_buckets=vectors,
        )
        if _passes_filter(row, filters):
            rows.append(row)
    return tuple(sorted(rows, key=lambda item: item.row_key))
