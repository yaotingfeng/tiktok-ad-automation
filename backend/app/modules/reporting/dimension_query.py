"""按账户本地日查询；SQL 严格汇总，BC 只决定当前账户的可见性。"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import case, func
from sqlalchemy import select as sql_select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.models import AdObject, CampaignNameProjection
from app.modules.reporting.filters import (
    CONFIG_SORT_FIELDS,
    NAME_SORT_FIELDS,
    authorized_grants,
    compile_filter,
)
from app.modules.reporting.models import AccountBalanceObservation
from app.modules.reporting.schemas import (
    Availability,
    MetricVector,
    ReportingFilter,
    ReportRow,
)
from app.modules.reporting.store_models import (
    METRIC_COLUMNS,
    AccountReconciliationDay,
    AdReportDay,
    CampaignReportDay,
    ReportSyncState,
)


def bucket_key(vector: MetricVector) -> tuple[str]:
    # 所选日期已按各账户本地日解释，同币种跨时区仍可合计，标记 MIXED。
    return (vector.currency,)


def metric_vector(values, *, currency, timezone, unsupported=()) -> MetricVector:
    values = {
        name: None if name in unsupported else values.get(name)
        for name in METRIC_COLUMNS
    }
    states = {
        name: "UNSUPPORTED"
        if name in unsupported
        else "AVAILABLE"
        if value is not None
        else "MISSING"
        for name, value in values.items()
    }
    for name, numerator, denominator in [
        *(
            (f"roas_{d}", f"revenue_{d}", "spend")
            for d in ("d0", "d3", "d7", "d14", "d30", "total")
        ),
        ("ctr", "clicks", "impressions"),
        ("cost_per_ad_impression_event", "spend", "ad_impression_events"),
    ]:
        a, b = values.get(numerator), values.get(denominator)
        values[name] = a / b if a is not None and b else None
        states[name] = (
            "UNSUPPORTED"
            if numerator in unsupported or denominator in unsupported
            else "AVAILABLE"
            if values[name] is not None
            else "MISSING"
        )
    return MetricVector(
        currency=currency,
        timezone=timezone,
        attribution="default",
        values=values,
        availability=cast(dict[str, Availability], states),
    )


def aggregate_metrics(vectors: Sequence[MetricVector]) -> MetricVector:
    if not vectors or len({v.currency for v in vectors}) != 1:
        raise ValueError("incompatible currencies or empty metrics")
    values = {}
    unsupported = set()
    for name in METRIC_COLUMNS:
        members = [v.values.get(name) for v in vectors]
        states = [v.availability.get(name) for v in vectors]
        if "UNSUPPORTED" in states:
            unsupported.add(name)
        values[name] = (
            sum(cast(list[Decimal], members), Decimal(0))
            if all(v is not None for v in members)
            and all(s == "AVAILABLE" for s in states)
            else None
        )
    zones = {v.timezone for v in vectors}
    return metric_vector(
        values,
        currency=vectors[0].currency,
        timezone=next(iter(zones)) if len(zones) == 1 else "MIXED",
        unsupported=unsupported,
    )


def scoped_rows(session, model, *, tenant_id, advertisers):
    return session.exec(
        select(model).where(
            model.tenant_id == tenant_id, col(model.advertiser_id).in_(advertisers)
        )
    ).all()


def _aggregate(session, *, model, group_cols, tenant_id, advertisers, start, end):
    # COUNT(col) 与 COUNT(*) 不同表示至少一条 NULL，禁止部分求和冒充完整数据。
    columns = [col(getattr(model, name)) for name in group_cols]
    metadata = [
        func.array_agg(
            aggregate_order_by(
                col(getattr(model, name)),
                col(model.stat_date).desc(),
                col(model.fetched_at).desc(),
            )
        )
        .filter(col(getattr(model, name)).is_not(None))[1]
        .label(name)
        for name in (
            "campaign_id",
            "adgroup_id",
            "campaign_name",
            "adgroup_name",
            "ad_name",
            "main_material_name",
            "main_material_type",
        )
        if hasattr(model, name) and name not in group_cols
    ]
    stmt = (
        sql_select(
            *columns,
            col(model.currency),
            func.min(col(model.timezone)).label("zone_min"),
            func.max(col(model.timezone)).label("zone_max"),
            *metadata,
            *[
                case(
                    (
                        func.count(col(getattr(model, name))) == func.count(),
                        func.sum(col(getattr(model, name))),
                    )
                ).label(name)
                for name in METRIC_COLUMNS
            ],
        )
        .where(
            model.tenant_id == tenant_id,
            col(model.advertiser_id).in_(advertisers),
            model.stat_date >= start,
            model.stat_date <= end,
        )
        .group_by(*columns, col(model.currency))
    )
    return list(session.connection().execute(stmt).mappings().all())


def _coverage(states, *, advertisers, filters, kind="ad_day"):
    relevant = {
        (s.advertiser_id, s.stat_date): s
        for s in states
        if s.report_kind == kind and s.advertiser_id in advertisers
    }
    dates = [
        filters.start_date + timedelta(days=i)
        for i in range((filters.end_date - filters.start_date).days + 1)
    ]
    selected = [relevant.get((adv, day)) for adv in advertisers for day in dates]
    stale = sorted(
        {
            day.isoformat()
            for adv in advertisers
            for day in dates
            if (s := relevant.get((adv, day))) is None or s.status != "COMPLETE"
        }
    )
    failed = sorted(
        {s.stat_date.isoformat() for s in selected if s and s.status == "FAILED"}
    )
    updated = [s.last_success_at for s in selected if s and s.last_success_at]
    return {
        "status": "MISSING"
        if not any(selected)
        else "INCOMPLETE"
        if stale
        else "COMPLETE",
        "stale_dates": stale,
        "failed_dates": failed,
        "updated_at": max(updated).isoformat() if updated else None,
    }


def empty_metric_values(states, *, advertiser, filters, kind="ad_day"):
    # 用户裁定：完整覆盖的无事实对象才为零；缺覆盖与平台显式 NULL 不能填零。
    complete = (
        _coverage(states, advertisers={advertiser}, filters=filters, kind=kind)[
            "status"
        ]
        == "COMPLETE"
    )
    return {name: Decimal(0) if complete else None for name in METRIC_COLUMNS}


def latest_projections(
    rows: Sequence[CampaignNameProjection],
) -> dict[tuple[str, str], CampaignNameProjection]:
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


def drama_key(
    bc_id: str,
    advertiser_id: str,
    campaign_id: str,
    projection: CampaignNameProjection | None,
) -> str:
    if projection and projection.status == "VALID":
        # JSON 元组编码避免名称内 ':' 产生碰撞；账户不参与有效剧的 BC 范围身份。
        return f"{bc_id}:drama:" + json.dumps(
            [projection.provider_label, projection.drama_name],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return f"{bc_id}:external:" + json.dumps(
        [advertiser_id, campaign_id], separators=(",", ":")
    )


def _created(value: Any, timezone: str) -> date | None:
    try:
        if isinstance(value, (int, float)) or (
            isinstance(value, str) and value.isdigit()
        ):
            return datetime.fromtimestamp(float(value), ZoneInfo(timezone)).date()
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (
                parsed.replace(tzinfo=ZoneInfo(timezone))
                if parsed.tzinfo is None
                else parsed.astimezone(ZoneInfo(timezone))
            ).date()
    except ValueError, OverflowError, OSError:
        pass
    return None


def _number(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except ValueError, InvalidOperation:
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
        "budget_mode": str(config.get("budget_mode"))
        if config.get("budget_mode")
        else None,
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
                "campaign_name": _entity_name(
                    objects, advertiser_id, "campaign", campaign_id
                ),
                "adgroup_id": remote_id,
                "adgroup_name": entity.name or remote_id if entity else remote_id,
            }
        )
    elif kind == "ad":
        display.update(
            {
                "campaign_id": campaign_id,
                "campaign_name": _entity_name(
                    objects, advertiser_id, "campaign", campaign_id
                ),
                "adgroup_id": adgroup_id,
                "adgroup_name": _entity_name(
                    objects, advertiser_id, "adgroup", adgroup_id
                ),
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


def _entity_matches(
    entity: AdObject | None,
    projection: CampaignNameProjection | None,
    filters: ReportingFilter,
    timezone: str,
) -> bool:
    for allowed, value in (
        (filters.ad_types, entity.ad_type if entity else None),
        (filters.operation_statuses, entity.operation_status if entity else None),
        (filters.review_statuses, entity.review_status if entity else None),
    ):
        if allowed and value not in allowed:
            return False
    if (
        filters.naming_status
        and (projection.status if projection else "INVALID") != filters.naming_status
    ):
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
    for bound, lower in (
        (filters.min_target_roas, True),
        (filters.max_target_roas, False),
    ):
        if bound is not None and (
            target is None or (target < bound if lower else target > bound)
        ):
            return False
    return True


def _passes_filter(row: ReportRow, filters: ReportingFilter) -> bool:
    ids = {row.row_key, *(ref.remote_id for ref in row.refs)}
    ids.update(
        value for key, value in row.display.items() if key.endswith("_id") and value
    )
    if filters.ids and not ids.intersection(filters.ids):
        return False
    text = " ".join(
        [row.row_key, *sorted(ids), *(value for value in row.display.values() if value)]
    ).casefold()
    if filters.query and any(
        word.casefold() not in text for word in filters.query.split()
    ):
        return False
    bounds = (
        ("spend", filters.min_spend, filters.max_spend),
        ("roas_d0", filters.min_d0_roas, filters.max_d0_roas),
    )
    if any(low is not None or high is not None for _, low, high in bounds):
        return any(
            all(
                (
                    low is None
                    or (
                        v.values.get(name) is not None
                        and cast(Decimal, v.values[name]) >= low
                    )
                )
                and (
                    high is None
                    or (
                        v.values.get(name) is not None
                        and cast(Decimal, v.values[name]) <= high
                    )
                )
                for name, low, high in bounds
            )
            for v in row.metric_buckets
        )
    return True


def _sort_value(row: ReportRow, field: str) -> tuple[str, Decimal] | None:
    """返回 (币种, 数值)；同一行多口径或无值时返回 None，统一排到末尾。"""
    if field in CONFIG_SORT_FIELDS:
        number = _number(row.display.get(field)) if row.display.get(field) else None
        return (
            (
                row.display.get("balance_currency") or "" if field == "balance" else "",
                number,
            )
            if number is not None
            else None
        )
    if len(row.metric_buckets) != 1:
        return None
    vector = row.metric_buckets[0]
    value = vector.values.get(field)
    if value is None:
        return None
    # 金额类指标先按币种分组，避免把不同币种的金额直接比较大小。
    currency = (
        vector.currency
        if field
        in {
            "spend",
            "revenue_d0",
            "revenue_d3",
            "revenue_d7",
            "revenue_d14",
            "revenue_d30",
            "revenue_total",
            "cost_per_ad_impression_event",
        }
        else ""
    )
    return (currency, value)


def _sort_rows(
    rows: Sequence[ReportRow], filters: ReportingFilter
) -> tuple[ReportRow, ...]:
    """服务端稳定排序：row_key 作为并列值，翻页不会漏行或重复。

    无值（未同步、平台未提供、多口径）在升序和降序下都排在末尾，
    不把缺失当作零参与排序。
    """
    field = filters.sort_by
    descending = filters.sort_direction == "desc"
    ordered = sorted(rows, key=lambda row: row.row_key)
    if field == "row_key" and descending:
        ordered.reverse()
    if field == "row_key":
        return tuple(ordered)
    if field in NAME_SORT_FIELDS:

        def name_key(row: ReportRow) -> str | None:
            value = row.display.get(field) or (
                row.display.get("name") if field == "name" else None
            )
            return value.casefold() if isinstance(value, str) and value else None

        present = [row for row in ordered if name_key(row) is not None]
        missing = [row for row in ordered if name_key(row) is None]
        present.sort(key=lambda row: cast(str, name_key(row)), reverse=descending)
        return (*present, *missing)
    present_values = [(row, _sort_value(row, field)) for row in ordered]
    present = [(row, value) for row, value in present_values if value is not None]
    missing = [row for row, value in present_values if value is None]
    present.sort(key=lambda item: item[1], reverse=descending)
    return (*(row for row, _ in present), *missing)


def build_rows(
    session: Session, *, context: TenantContext, bc_id: str, filters: ReportingFilter
) -> tuple[ReportRow, ...]:
    compile_filter(filters)
    if filters.dimension == "material":
        from app.modules.reporting.material_query import build_material_rows

        return build_material_rows(
            session, context=context, bc_id=bc_id, filters=filters
        )
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
    states = session.exec(
        select(ReportSyncState).where(
            ReportSyncState.tenant_id == context.tenant_id,
            col(ReportSyncState.advertiser_id).in_(advertisers),
            ReportSyncState.stat_date >= filters.start_date,
            ReportSyncState.stat_date <= filters.end_date,
        )
    ).all()
    kind = "campaign" if filters.dimension == "drama" else filters.dimension
    model = CampaignReportDay if kind in {"account", "campaign"} else AdReportDay
    group_cols = ["advertiser_id"] + ([] if kind == "account" else [f"{kind}_id"])
    grouped_facts = _aggregate(
        session,
        model=model,
        group_cols=group_cols,
        tenant_id=context.tenant_id,
        advertisers=advertisers,
        start=filters.start_date,
        end=filters.end_date,
    )
    facts = defaultdict(list)
    for fact in grouped_facts:
        adv = fact["advertiser_id"]
        facts[(adv, adv if kind == "account" else fact[f"{kind}_id"])].append(fact)
    identities = set(facts) | {(adv, rid) for adv, k, rid in objects if k == kind}
    if kind == "account":
        identities.update((adv, adv) for adv in advertisers)
    reconciliations = (
        session.exec(
            select(AccountReconciliationDay).where(
                AccountReconciliationDay.tenant_id == context.tenant_id,
                col(AccountReconciliationDay.advertiser_id).in_(advertisers),
                AccountReconciliationDay.stat_date >= filters.start_date,
                AccountReconciliationDay.stat_date <= filters.end_date,
            )
        ).all()
        if kind == "account"
        else []
    )
    balances = {}
    if kind == "account":
        for observation in session.exec(
            select(AccountBalanceObservation)
            .where(
                AccountBalanceObservation.tenant_id == context.tenant_id,
                col(AccountBalanceObservation.advertiser_id).in_(advertisers),
            )
            .distinct(col(AccountBalanceObservation.advertiser_id))
            .order_by(
                col(AccountBalanceObservation.advertiser_id),
                col(AccountBalanceObservation.observed_at).desc(),
                col(AccountBalanceObservation.id).desc(),
            )
        ):
            balances.setdefault(observation.advertiser_id, observation)
    groups = defaultdict(list)
    group_accounts = defaultdict(set)
    for adv, rid in sorted(identities):
        entity = objects.get((adv, kind, rid))
        items = facts.get((adv, rid), [])
        metadata = items[0] if items else {}
        campaign_id = rid if kind == "campaign" else metadata.get("campaign_id")
        if entity and kind == "adgroup":
            campaign_id = entity.parent_remote_id or campaign_id
        if entity and kind == "ad":
            parent = objects.get((adv, "adgroup", entity.parent_remote_id or ""))
            campaign_id = parent.parent_remote_id if parent else campaign_id
        projection = projections.get((adv, campaign_id))
        if not _entity_matches(entity, projection, filters, accounts[adv].timezone):
            continue
        key = (
            drama_key(bc_id, adv, rid, projection)
            if filters.dimension == "drama"
            else f"{adv}:{kind}:{rid}"
        )
        name = (
            accounts[adv].name or rid
            if kind == "account"
            else (
                entity.name
                if entity and entity.name
                else metadata.get(f"{kind}_name") or rid
            )
        )
        display = _hierarchy_display(
            accounts=accounts,
            objects=objects,
            advertiser_id=adv,
            kind=kind,
            remote_id=rid,
            entity=entity,
        )
        # 目录尚未同步时以报表中的父级身份与名称补齐，不能丢弃已删除广告的历史消耗。
        for parent_kind in ("campaign", "adgroup", "ad"):
            if parent_kind not in display and f"{parent_kind}_id" not in display:
                continue
            parent_id = display.get(f"{parent_kind}_id") or metadata.get(
                f"{parent_kind}_id"
            )
            if parent_id:
                obj = objects.get((adv, parent_kind, parent_id))
                display[f"{parent_kind}_id"] = parent_id
                display[f"{parent_kind}_name"] = (
                    obj.name
                    if obj and obj.name
                    else metadata.get(f"{parent_kind}_name") or parent_id
                )
        if filters.dimension == "drama":
            name = (
                projection.drama_name
                if projection and projection.status == "VALID"
                else name
            )
            display.update(drama_name=name)
        display.update(
            name=name,
            remote_id=rid,
            advertiser_id=adv,
            provider=projection.provider_label if projection else None,
            naming_status=projection.status if projection else "INVALID",
            ad_type=entity.ad_type if entity else None,
            operation_status=entity.operation_status if entity else None,
            review_status=entity.review_status if entity else None,
        )
        if kind == "account":
            balance = balances.get(adv)
            # 余额是独立时点观测；资金池余额必须标记共享，不纳入期间指标合计。
            display.update(
                balance=str(balance.amount)
                if balance
                and balance.availability == "AVAILABLE"
                and balance.amount is not None
                else None,
                balance_currency=balance.currency if balance else None,
                balance_scope=balance.balance_scope if balance else "UNKNOWN",
                balance_updated_at=balance.observed_at.isoformat() if balance else None,
            )
        own_recs = [r for r in reconciliations if r.advertiser_id == adv]
        if any(
            a is not None and b is not None and abs(a - b) > Decimal("0.01")
            for r in own_recs
            for a, b in (
                (r.account_spend, r.ad_sum_spend),
                (r.account_revenue_total, r.ad_sum_revenue_total),
            )
        ):
            display["reconciliation"] = "DIFF"
            display["account_report_spend"] = (
                str(sum((cast(Decimal, r.account_spend) for r in own_recs), Decimal(0)))
                if all(r.account_spend is not None for r in own_recs)
                else None
            )
        vectors = tuple(
            metric_vector(
                item,
                currency=item["currency"],
                timezone=item["zone_min"]
                if item["zone_min"] == item["zone_max"]
                else "MIXED",
            )
            for item in items
        )
        if not vectors:
            vectors = (
                metric_vector(
                    empty_metric_values(states, advertiser=adv, filters=filters),
                    currency=accounts[adv].currency,
                    timezone=accounts[adv].timezone,
                ),
            )
        refs = (
            ()
            if kind == "account"
            else (EntityRef(context.tenant_id, adv, cast(Any, kind), rid),)
        )
        groups[key].append(
            ReportRow(
                row_key=key,
                display=display,
                refs=refs,
                metric_buckets=vectors,
                directory_versions={f"{adv}:{rid}": entity.published_version}
                if entity
                else {},
            )
        )
        group_accounts[key].add(adv)
    result = []
    for key, members in groups.items():
        buckets = defaultdict(list)
        for row in members:
            for vector in row.metric_buckets:
                buckets[bucket_key(vector)].append(vector)
        refs = tuple(ref for row in members for ref in row.refs)
        row = members[0].model_copy(
            update={
                "refs": refs,
                "metric_buckets": tuple(
                    aggregate_metrics(values) for _, values in sorted(buckets.items())
                ),
                "coverage": _coverage(
                    states, advertisers=group_accounts[key], filters=filters
                ),
                "membership_digest": sha256(
                    json.dumps(
                        sorted(
                            (
                                str(ref.tenant_id),
                                ref.advertiser_id,
                                ref.kind,
                                ref.remote_id,
                            )
                            for ref in refs
                        )
                    ).encode()
                ).hexdigest(),
                "directory_versions": {
                    k: v
                    for member in members
                    for k, v in member.directory_versions.items()
                },
            }
        )
        if _passes_filter(row, filters):
            result.append(row)
    return _sort_rows(result, filters)
