# backend/app/modules/reporting/publish.py
"""整份替换发布：某个“账户 × 报表 × 日期段”全部分页成功后，在一个事务内
先删旧行再写新行。平台未返回的行就是当天没有数据，删除即正确结果。

汇总规则：任一成员指标为 NULL（平台 '-'）时合计为 NULL，绝不显示部分合计。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, insert, text
from sqlmodel import Session

from app.core.errors import DomainError
from app.modules.reporting.metrics import (
    basic_metric_values,
    material_metric_values,
    parse_decimal,
    parse_local_day,
    parse_local_hour,
)
from app.modules.reporting.store_models import (
    METRIC_COLUMNS,
    AccountReconciliationDay,
    AdReportDay,
    CampaignReportDay,
    CampaignReportDayUtc,
    CampaignReportHour,
    MaterialReportDay,
    ReportSyncState,
)


@dataclass(frozen=True)
class AccountMeta:
    currency: str
    timezone: str


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _account(row: dict, accounts: dict[str, AccountMeta]) -> str:
    advertiser = (row.get("metrics") or {}).get("advertiser_id") or (
        row.get("dimensions") or {}
    ).get("advertiser_id")
    if advertiser is None and len(accounts) == 1:
        advertiser = next(iter(accounts))
    if advertiser not in accounts:
        raise DomainError("report_row_invalid", "报表行账户不在本次请求范围内")
    return str(advertiser)


def _in_range(day: date, start: date, end: date) -> date:
    if not start <= day <= end:
        raise DomainError("report_row_invalid", "报表行日期超出请求范围")
    return day


def _set_state(
    session,
    *,
    tenant_id,
    advertiser_ids,
    kind,
    days,
    counts: dict[tuple[str, date], int],
):
    now = datetime.now(UTC)
    for advertiser in advertiser_ids:
        for day in days:
            state = session.get(ReportSyncState, (tenant_id, advertiser, kind, day))
            if state is None:
                state = ReportSyncState(
                    tenant_id=tenant_id,
                    advertiser_id=advertiser,
                    report_kind=kind,
                    stat_date=day,
                    status="COMPLETE",
                )
                session.add(state)
            state.status, state.error_code = "COMPLETE", None
            state.row_count = counts.get((advertiser, day), 0)
            state.last_success_at = state.updated_at = now


def mark_failed(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_ids: Sequence[str],
    kind: str,
    start: date,
    end: date,
    error_code: str,
) -> None:
    """失败不删除旧数据：已有完整数据标为 STALE，从未成功的标为 FAILED。"""
    now = datetime.now(UTC)
    for advertiser in advertiser_ids:
        for day in _days(start, end):
            state = session.get(ReportSyncState, (tenant_id, advertiser, kind, day))
            if state is None:
                session.add(
                    ReportSyncState(
                        tenant_id=tenant_id,
                        advertiser_id=advertiser,
                        report_kind=kind,
                        stat_date=day,
                        status="FAILED",
                        error_code=error_code,
                        updated_at=now,
                    )
                )
            else:
                state.status = "STALE" if state.last_success_at else "FAILED"
                state.error_code, state.updated_at = error_code, now
    session.flush()


def _rollup_sql() -> str:
    # COUNT(col)=COUNT(*) 才求和：任一成员为 NULL 则系列合计为 NULL。
    cols = ", ".join(
        f"CASE WHEN COUNT({c}) = COUNT(*) THEN SUM({c}) END AS {c}"
        for c in METRIC_COLUMNS
    )
    return f"""
        INSERT INTO campaign_report_day (tenant_id, advertiser_id, stat_date, campaign_id, campaign_name,
            {", ".join(METRIC_COLUMNS)}, currency, timezone, fetched_at, sync_job_id)
        SELECT tenant_id, advertiser_id, stat_date, campaign_id, MAX(campaign_name), {cols},
               MAX(currency), MAX(timezone), MAX(fetched_at), :job_id
        FROM ad_report_day
        WHERE tenant_id = :tenant_id AND advertiser_id = ANY(:advertisers)
          AND stat_date BETWEEN :start AND :end
        GROUP BY tenant_id, advertiser_id, stat_date, campaign_id
    """


def publish_ad_day(
    session: Session,
    *,
    tenant_id: UUID,
    job_id: UUID,
    accounts: dict[str, AccountMeta],
    start: date,
    end: date,
    rows: Sequence[dict],
    campaign_ids: tuple[str, ...] = (),
) -> int:
    now, records, counts = datetime.now(UTC), [], {}
    for row in rows:
        advertiser = _account(row, accounts)
        dims, metrics = row.get("dimensions") or {}, row.get("metrics") or {}
        day = _in_range(parse_local_day(dims.get("stat_time_day")), start, end)
        ad_id, campaign, adgroup = (
            dims.get("ad_id_v2"),
            metrics.get("campaign_id"),
            metrics.get("adgroup_id"),
        )
        if not ad_id or not campaign or not adgroup:
            raise DomainError("report_row_invalid", "广告行缺少广告、系列或广告组 ID")
        if campaign_ids and campaign not in campaign_ids:
            raise DomainError("report_row_invalid", "按系列分批结果包含范围外系列")
        meta = accounts[advertiser]
        records.append(
            {
                "tenant_id": tenant_id,
                "advertiser_id": advertiser,
                "stat_date": day,
                "ad_id": str(ad_id),
                "campaign_id": str(campaign),
                "adgroup_id": str(adgroup),
                "campaign_automation_type": metrics.get("campaign_automation_type"),
                "ad_name": metrics.get("ad_name"),
                "campaign_name": metrics.get("campaign_name"),
                "adgroup_name": metrics.get("adgroup_name"),
                "currency": meta.currency,
                "timezone": meta.timezone,
                "fetched_at": now,
                "sync_job_id": job_id,
                **basic_metric_values(metrics),
            }
        )
        counts[(advertiser, day)] = counts.get((advertiser, day), 0) + 1
    advertisers = sorted(accounts)
    statement = delete(AdReportDay).where(
        AdReportDay.tenant_id == tenant_id,
        AdReportDay.advertiser_id.in_(advertisers),
        AdReportDay.stat_date.between(start, end),
    )
    if campaign_ids:
        statement = statement.where(AdReportDay.campaign_id.in_(campaign_ids))
    session.execute(statement)
    if records:
        session.execute(insert(AdReportDay), records)
    session.execute(
        delete(CampaignReportDay).where(
            CampaignReportDay.tenant_id == tenant_id,
            CampaignReportDay.advertiser_id.in_(advertisers),
            CampaignReportDay.stat_date.between(start, end),
        )
    )
    session.execute(
        text(_rollup_sql()),
        {
            "tenant_id": tenant_id,
            "advertisers": advertisers,
            "start": start,
            "end": end,
            "job_id": job_id,
        },
    )
    if not campaign_ids:
        _set_state(
            session,
            tenant_id=tenant_id,
            advertiser_ids=advertisers,
            kind="ad_day",
            days=_days(start, end),
            counts=counts,
        )
    session.flush()
    return len(records)


def _publish_simple(
    session,
    *,
    model,
    kind,
    key_builder,
    tenant_id,
    job_id,
    accounts,
    start,
    end,
    rows,
    value_parser,
    time_filter,
) -> int:
    now, records, counts = datetime.now(UTC), [], {}
    for row in rows:
        advertiser = _account(row, accounts)
        meta = accounts[advertiser]
        keys, day = key_builder(row, meta)
        records.append(
            {
                "tenant_id": tenant_id,
                "advertiser_id": advertiser,
                "currency": meta.currency,
                "timezone": meta.timezone,
                "fetched_at": now,
                "sync_job_id": job_id,
                **keys,
                **value_parser(row.get("metrics") or {}),
            }
        )
        counts[(advertiser, day)] = counts.get((advertiser, day), 0) + 1
    advertisers = sorted(accounts)
    session.execute(
        delete(model).where(
            model.tenant_id == tenant_id,
            model.advertiser_id.in_(advertisers),
            time_filter(model),
        )
    )
    if records:
        session.execute(insert(model), records)
    _set_state(
        session,
        tenant_id=tenant_id,
        advertiser_ids=advertisers,
        kind=kind,
        days=_days(start, end),
        counts=counts,
    )
    session.flush()
    return len(records)


def publish_campaign_hour(session, *, tenant_id, job_id, accounts, day, rows) -> int:
    def keys(row, meta):
        dims = row.get("dimensions") or {}
        # 先按原始账户本地日期校验，不能把邻日小时写进当天发布窗口。
        _in_range(parse_local_day(dims.get("stat_time_hour")), day, day)
        if not dims.get("campaign_id"):
            raise DomainError("report_row_invalid", "系列行缺少系列 ID")
        hour = parse_local_hour(dims.get("stat_time_hour"), meta.timezone)
        return {"stat_hour": hour, "campaign_id": str(dims.get("campaign_id"))}, day

    def values(metrics):
        parsed = basic_metric_values(metrics)
        return {c: parsed[c] for c in METRIC_COLUMNS}

    # 各账户“本地这一天”的 UTC 边界不同；按每个账户的时区分别删除。
    def window(model):
        clauses = []
        for advertiser, meta in accounts.items():
            start_hour = parse_local_hour(f"{day} 00:00:00", meta.timezone)
            end_hour = parse_local_hour(
                f"{day + timedelta(days=1)} 00:00:00", meta.timezone
            )
            clauses.append(
                (model.advertiser_id == advertiser)
                & (model.stat_hour >= start_hour)
                & (model.stat_hour < end_hour)
            )
        from sqlalchemy import or_

        return or_(*clauses)

    return _publish_simple(
        session,
        model=CampaignReportHour,
        kind="campaign_hour",
        key_builder=keys,
        tenant_id=tenant_id,
        job_id=job_id,
        accounts=accounts,
        start=day,
        end=day,
        rows=rows,
        value_parser=values,
        time_filter=window,
    )


def publish_campaign_day_utc(
    session, *, tenant_id, job_id, accounts, start, end, rows
) -> int:
    def keys(row, _meta):
        dims = row.get("dimensions") or {}
        day = _in_range(parse_local_day(dims.get("stat_time_day")), start, end)
        if not dims.get("campaign_id"):
            raise DomainError("report_row_invalid", "系列行缺少系列 ID")
        return {"stat_date": day, "campaign_id": str(dims["campaign_id"])}, day

    return _publish_simple(
        session,
        model=CampaignReportDayUtc,
        kind="campaign_day_utc",
        key_builder=keys,
        tenant_id=tenant_id,
        job_id=job_id,
        accounts=accounts,
        start=start,
        end=end,
        rows=rows,
        value_parser=basic_metric_values,
        time_filter=lambda m: m.stat_date.between(start, end),
    )


def publish_material_day(
    session, *, tenant_id, job_id, accounts, start, end, rows
) -> int:
    def keys(row, _meta):
        dims, metrics = row.get("dimensions") or {}, row.get("metrics") or {}
        day = _in_range(parse_local_day(dims.get("stat_time_day")), start, end)
        if not dims.get("main_material_id") or not dims.get("main_material_type"):
            raise DomainError("report_row_invalid", "素材行缺少主素材身份")
        return {
            "stat_date": day,
            "main_material_id": str(dims["main_material_id"]),
            "main_material_type": str(dims["main_material_type"]),
            "main_material_name": metrics.get("main_material_name"),
        }, day

    return _publish_simple(
        session,
        model=MaterialReportDay,
        kind="material_day",
        key_builder=keys,
        tenant_id=tenant_id,
        job_id=job_id,
        accounts=accounts,
        start=start,
        end=end,
        rows=rows,
        value_parser=material_metric_values,
        time_filter=lambda m: m.stat_date.between(start, end),
    )


def publish_account_reconciliation(
    session, *, tenant_id, accounts, start, end, rows
) -> None:
    """账户报表与同期广告汇总比对，结果只用于页面提示。"""
    reported: dict[tuple[str, date], dict[str, Any]] = {}
    for row in rows:
        advertiser = _account(row, accounts)
        day = _in_range(
            parse_local_day((row.get("dimensions") or {}).get("stat_time_day")),
            start,
            end,
        )
        metrics = row.get("metrics") or {}
        reported[(advertiser, day)] = {
            "spend": parse_decimal(metrics.get("spend")),
            "total": parse_decimal(
                metrics.get("native_growth_total_ad_impression_value")
            ),
        }
    sums = session.execute(
        text("""
        SELECT advertiser_id, stat_date,
               CASE WHEN COUNT(spend)=COUNT(*) THEN SUM(spend) END,
               CASE WHEN COUNT(revenue_total)=COUNT(*) THEN SUM(revenue_total) END
        FROM ad_report_day WHERE tenant_id=:t AND advertiser_id = ANY(:a) AND stat_date BETWEEN :s AND :e
        GROUP BY advertiser_id, stat_date"""),
        {"t": tenant_id, "a": sorted(accounts), "s": start, "e": end},
    ).all()
    summed = {(a, d): (s, r) for a, d, s, r in sums}
    now = datetime.now(UTC)
    for advertiser in accounts:
        for day in _days(start, end):
            rep = reported.get((advertiser, day), {"spend": None, "total": None})
            ad_spend, ad_total = summed.get((advertiser, day), (None, None))
            session.merge(
                AccountReconciliationDay(
                    tenant_id=tenant_id,
                    advertiser_id=advertiser,
                    stat_date=day,
                    account_spend=rep["spend"],
                    ad_sum_spend=ad_spend,
                    account_revenue_total=rep["total"],
                    ad_sum_revenue_total=ad_total,
                    checked_at=now,
                )
            )
    session.flush()
