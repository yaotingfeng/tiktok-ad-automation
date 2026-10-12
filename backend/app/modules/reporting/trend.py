"""本地事实趋势。

页面趋势与详情趋势共用：按层级和对象 ID 从本地日/小时事实聚合，不调用
TikTok。2026-10-05 曾为首屏性能把趋势移出快照，因此趋势单独按需读取。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy import select as sql_select
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.reporting.dimension_query import merge_metric_buckets, metric_vector
from app.modules.reporting.filters import authorized_grants
from app.modules.reporting.schemas import MetricVector, TrendPoint, TrendPublic
from app.modules.reporting.store_models import (
    METRIC_COLUMNS,
    AdReportDay,
    CampaignReportDay,
    CampaignReportHour,
    MaterialReportDay,
    ReportSyncState,
)

MAX_DAY_SPAN = 90
MAX_HOUR_SPAN = 2
MAX_IDS = 200
Coverage = Literal["COMPLETE", "INCOMPLETE", "MISSING"]
# 层级 -> (日事实表, 对象 ID 列, 覆盖状态种类)；账户层级的 ids 即账户 ID。
_DAY_MODELS: dict[str, tuple[Any, str, str]] = {
    "account": (CampaignReportDay, "advertiser_id", "ad_day"),
    "campaign": (CampaignReportDay, "campaign_id", "ad_day"),
    "adgroup": (AdReportDay, "adgroup_id", "ad_day"),
    "ad": (AdReportDay, "ad_id", "ad_day"),
    "material": (MaterialReportDay, "main_material_id", "material_day"),
}
# 素材报表平台不提供 D3、D30，与素材维度行一致标为不支持而不是缺失。
_MATERIAL_UNSUPPORTED = frozenset({"revenue_d3", "revenue_d30"})


def _invalid(detail: str) -> HTTPException:
    return HTTPException(422, detail=detail)


def build_trend(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    level: str,
    advertiser_ids: tuple[str, ...],
    ids: tuple[str, ...],
    start: date,
    end: date,
    granularity: str,
) -> TrendPublic:
    if end < start:
        raise _invalid("trend_range_invalid")
    span = (end - start).days + 1
    if len(ids) > MAX_IDS:
        raise _invalid("trend_ids_too_many")
    if granularity == "hour":
        if level not in ("account", "campaign"):
            raise _invalid("trend_hour_level_unsupported")
        if span > MAX_HOUR_SPAN:
            raise _invalid("trend_range_too_long")
    elif span > MAX_DAY_SPAN:
        raise _invalid("trend_range_too_long")
    # 账户层级的 ids 本身就是账户，与 advertiser_ids 一样必须在授权内。
    requested = set(advertiser_ids) | (set(ids) if level == "account" else set())
    grants = {
        g.advertiser_id
        for g in authorized_grants(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_ids=tuple(sorted(requested)),
        )
    }
    # 显式请求的账户必须全部在当前 BC 授权范围内；不在范围直接拒绝，不返回部分数据。
    if requested and not requested <= grants:
        raise HTTPException(403, detail="trend_scope_forbidden")
    # 覆盖范围：显式请求的账户，否则为全部授权账户；不按 ids 反推所属账户。
    scope = grants
    model, id_col, state_kind = _DAY_MODELS[level]
    if granularity == "hour":
        # 小时数据有独立的同步状态（campaign_hour），按其账户本地日记录。
        model, state_kind = CampaignReportHour, "campaign_hour"
        time_col = col(CampaignReportHour.stat_hour)
    else:
        time_col = col(model.stat_date)
    stmt = (
        sql_select(
            time_col.label("at"),
            col(model.currency),
            col(model.timezone),
            *[
                func.sum(col(getattr(model, name))).label(name)
                for name in METRIC_COLUMNS
            ],
            *[
                func.count(col(getattr(model, name))).label(f"n_{name}")
                for name in METRIC_COLUMNS
            ],
            func.count().label("n_rows"),
        )
        .where(
            col(model.tenant_id) == context.tenant_id,
            col(model.advertiser_id).in_(scope),
        )
        .group_by(time_col, col(model.currency), col(model.timezone))
        .order_by(time_col)
    )
    if granularity == "hour":
        # 小时事实以 UTC 时刻存储；先按前后各放宽一天粗筛，再逐行按账户本地日过滤。
        stmt = stmt.where(
            time_col >= datetime.combine(start - timedelta(days=1), time(), UTC),
            time_col < datetime.combine(end + timedelta(days=2), time(), UTC),
        )
    else:
        stmt = stmt.where(time_col >= start, time_col <= end)
    if ids:
        stmt = stmt.where(col(getattr(model, id_col)).in_(ids))
    rows = session.connection().execute(stmt).mappings().all()

    unsupported = _MATERIAL_UNSUPPORTED if level == "material" else frozenset()
    by_point: dict[str, list[MetricVector]] = defaultdict(list)
    point_days: dict[str, set[date]] = defaultdict(set)
    zones: set[str] = set()
    for row in rows:
        if granularity == "hour":
            instant = cast(datetime, row["at"]).astimezone(UTC)
            local_day = instant.astimezone(ZoneInfo(row["timezone"])).date()
            if not start <= local_day <= end:
                continue
            key = instant.isoformat(timespec="minutes")
        else:
            local_day = cast(date, row["at"])
            key = local_day.isoformat()
        values = {
            name: row[name] if row[f"n_{name}"] else None for name in METRIC_COLUMNS
        }
        # 部分合计规则：某点内有成员该列为 NULL 时不当 0，已有值相加并标 PARTIAL。
        partial = {
            name for name in METRIC_COLUMNS if 0 < row[f"n_{name}"] < row["n_rows"]
        }
        zones.add(row["timezone"])
        point_days[key].add(local_day)
        by_point[key].append(
            metric_vector(
                values,
                currency=row["currency"],
                timezone=row["timezone"],
                unsupported=unsupported,
                partial=partial,
            )
        )

    states = session.exec(
        select(ReportSyncState).where(
            ReportSyncState.tenant_id == context.tenant_id,
            col(ReportSyncState.advertiser_id).in_(scope),
            ReportSyncState.report_kind == state_kind,
            col(ReportSyncState.stat_date) >= start,
            col(ReportSyncState.stat_date) <= end,
        )
    ).all()
    complete_days: dict[date, set[str]] = defaultdict(set)
    for state in states:
        if state.status == "COMPLETE":
            complete_days[state.stat_date].add(state.advertiser_id)

    def day_coverage(day: date, has_facts: bool) -> Coverage:
        done = complete_days.get(day, set())
        if scope and done >= scope:
            return "COMPLETE"
        return "INCOMPLETE" if done or has_facts else "MISSING"

    if granularity == "day":
        keys = [(start + timedelta(days=i)).isoformat() for i in range(span)]
    else:
        keys = sorted(by_point)
    points = []
    for key in keys:
        vectors = by_point.get(key, [])
        days = point_days.get(key) or {date.fromisoformat(key[:10])}
        covers = {day_coverage(day, bool(vectors)) for day in days}
        coverage: Coverage = (
            "COMPLETE"
            if covers == {"COMPLETE"}
            else "INCOMPLETE"
            if covers & {"COMPLETE", "INCOMPLETE"}
            else "MISSING"
        )
        points.append(
            TrendPoint(
                at=key,
                # 与汇总/行内一致：该点覆盖不完整时桶内已有值统一标 PARTIAL。
                buckets=merge_metric_buckets([(vectors, coverage != "COMPLETE")]),
                coverage=coverage,
            )
        )
    return TrendPublic(
        level=cast(Any, level),
        granularity=cast(Any, granularity),
        timezone=next(iter(zones)) if len(zones) == 1 else "MIXED" if zones else "UTC",
        points=tuple(points),
    )
