# backend/tests/modules/reporting/test_dimension_query.py
from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.modules.reporting.dimension_query import build_rows
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.sync_conftest import ad_row

D = date(2026, 10, 5)


def _filters(dimension, **kw):
    return ReportingFilter(dimension=dimension, start_date=D, end_date=D, **kw)


def _seed(session, seed):
    publish_ad_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={
            "acc-1": AccountMeta("USD", "Asia/Hong_Kong"),
            "acc-2": AccountMeta("USD", "UTC"),
        },
        start=D,
        end=D,
        rows=[
            ad_row("a1", D, campaign="c1", spend="10.00", d0="5.00", total="6.00"),
            ad_row("a2", D, campaign="c1", spend="5.00", d0="1.00", total="1.00"),
            ad_row(
                "b1", D, campaign="c9", spend="2.00", d0="-", total="2.00", adv="acc-2"
            ),
        ],
    )


def test_levels_add_up_and_roas_is_sum_over_sum(session, sync_seed):
    _seed(session, sync_seed)
    ctx = sync_seed.context
    camp = {
        r.display["remote_id"]: r
        for r in build_rows(
            session, context=ctx, bc_id="bc-a", filters=_filters("campaign")
        )
    }
    assert camp["c1"].metric_buckets[0].values["spend"] == Decimal("15.00")
    assert camp["c1"].metric_buckets[0].values["roas_d0"] == Decimal("6.00") / Decimal(
        "15.00"
    )
    acct = {
        r.display["remote_id"]: r
        for r in build_rows(
            session, context=ctx, bc_id="bc-a", filters=_filters("account")
        )
    }
    assert acct["acc-1"].metric_buckets[0].values["spend"] == Decimal("15.00")
    assert (
        acct["acc-2"].metric_buckets[0].values["revenue_d0"] is None
    )  # 有成员缺失 → 合计为空


def test_shared_account_visible_in_both_bcs(session, sync_seed):
    _seed(session, sync_seed)
    rows_b = build_rows(
        session, context=sync_seed.context, bc_id="bc-b", filters=_filters("account")
    )
    assert [r.display["remote_id"] for r in rows_b] == ["acc-2"]


def test_every_metric_sorts_with_null_last(session, sync_seed):
    _seed(session, sync_seed)
    rows = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=_filters("ad", sort_by="roas_d0", sort_direction="desc"),
    )
    assert [r.display["remote_id"] for r in rows] == ["a1", "a2", "b1"]


def test_missing_sync_state_marks_incomplete(session, sync_seed):
    _seed(session, sync_seed)
    rows = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="account", start_date=date(2026, 10, 4), end_date=D
        ),
    )
    assert all(r.coverage["status"] == "INCOMPLETE" for r in rows)  # 10-04 未同步


def test_local_day_null_is_strict_and_names_fall_back_to_facts(session, sync_seed):
    _seed(session, sync_seed)
    day = date(2026, 10, 6)
    raw = ad_row("a1", day, campaign="c1", spend="3", d0="-", total="0")
    raw["metrics"].update(
        ad_name="广告本名", campaign_name="系列本名", adgroup_name="广告组本名"
    )
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        start=day,
        end=day,
        rows=[raw],
    )
    rows = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(dimension="ad", start_date=D, end_date=day),
    )
    row = next(r for r in rows if r.display["remote_id"] == "a1")
    assert row.metric_buckets[0].values["spend"] == Decimal(13)
    assert row.metric_buckets[0].values["revenue_d0"] is None
    assert row.metric_buckets[0].values["roas_d0"] is None
    assert row.display["name"] == "广告本名"
    assert row.display["campaign_name"] == "系列本名"
    assert row.display["adgroup_name"] == "广告组本名"


def test_drama_merges_same_currency_timezones_partial_sums_and_avoids_delimiter_collision(
    session, sync_seed
):
    from app.modules.ads.models import CampaignNameProjection

    _seed(session, sync_seed)
    for adv, campaign, provider, drama in (
        ("acc-1", "c1", "P:A", "D"),
        ("acc-2", "c9", "P", "A:D"),
    ):
        session.add(
            CampaignNameProjection(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                campaign_remote_id=campaign,
                name_revision=1,
                raw_name="test",
                provider_label=provider,
                drama_name=drama,
                status="VALID",
                parser_revision=1,
                grouping_revision=1,
            )
        )
    session.flush()
    rows = build_rows(
        session, context=sync_seed.context, bc_id="bc-a", filters=_filters("drama")
    )
    assert len(rows) == 2
    for p in session.exec(
        __import__("sqlmodel")
        .select(CampaignNameProjection)
        .where(CampaignNameProjection.tenant_id == sync_seed.tenant_id)
    ).all():
        p.provider_label, p.drama_name = "共同版权方", "共同剧"
    session.flush()
    rows = build_rows(
        session, context=sync_seed.context, bc_id="bc-a", filters=_filters("drama")
    )
    assert len(rows) == 1 and len(rows[0].metric_buckets) == 1
    assert rows[0].metric_buckets[0].timezone == "MIXED"
    assert rows[0].metric_buckets[0].values["spend"] == Decimal(17)
    # 一个成员 D0 回收为空：其余成员照常合计，标 PARTIAL 而不是整列置空。
    assert rows[0].metric_buckets[0].values["revenue_d0"] == Decimal(6)
    assert rows[0].metric_buckets[0].availability["revenue_d0"] == "PARTIAL"
    assert rows[0].metric_buckets[0].availability["spend"] == "AVAILABLE"
    assert rows[0].coverage["status"] == "COMPLETE"


def test_reconciliation_keeps_ad_sum_and_shows_account_original(session, sync_seed):
    from datetime import UTC, datetime

    from app.modules.reporting.store_models import AccountReconciliationDay

    _seed(session, sync_seed)
    session.add(
        AccountReconciliationDay(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            stat_date=D,
            account_spend=Decimal("16"),
            ad_sum_spend=Decimal("15"),
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()
    rows = build_rows(
        session, context=sync_seed.context, bc_id="bc-a", filters=_filters("account")
    )
    row = next(r for r in rows if r.display["remote_id"] == "acc-1")
    assert row.display["reconciliation"] == "DIFF"
    assert Decimal(row.display["account_report_spend"]) == Decimal(16)
    assert row.metric_buckets[0].values["spend"] == Decimal(15)


def test_summary_keeps_currencies_separate_and_derives_ratios():
    from app.modules.reporting.dimension_query import metric_vector
    from app.modules.reporting.queries import _summary
    from app.modules.reporting.schemas import ReportRow

    rows = tuple(
        ReportRow(
            row_key=str(i),
            metric_buckets=(
                metric_vector(
                    {"spend": Decimal(spend), "revenue_d0": Decimal(revenue)},
                    currency=currency,
                    timezone=timezone,
                ),
            ),
        )
        for i, (currency, timezone, spend, revenue) in enumerate(
            (
                ("USD", "UTC", 10, 1),
                ("USD", "Asia/Hong_Kong", 20, 20),
                ("EUR", "UTC", 100, 1),
            )
        )
    )
    totals = {v["currency"]: v for v in _summary(rows)["buckets"]}
    assert len(totals) == 2
    assert totals["USD"]["timezone"] == "MIXED"
    assert Decimal(totals["USD"]["values"]["roas_d0"]) == Decimal("0.7")


def test_cpm_and_cpc_are_recomputed_from_totals(session, sync_seed):
    _seed(session, sync_seed)
    camp = {
        r.display["remote_id"]: r
        for r in build_rows(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=_filters("campaign"),
        )
    }
    values = camp["c1"].metric_buckets[0].values
    # ad_row 每行固定 impressions=10、clicks=1；c1 合计消耗 15、展示 20、点击 2。
    assert values["cpm"] == Decimal("15.00") / Decimal("20") * 1000
    assert values["cpc"] == Decimal("15.00") / Decimal("2")


def test_cpm_sorts_desc(session, sync_seed):
    _seed(session, sync_seed)
    rows = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=_filters("ad", sort_by="cpm", sort_direction="desc"),
    )
    assert [r.display["remote_id"] for r in rows] == ["a1", "a2", "b1"]


def test_account_row_equals_sum_of_its_campaigns(session, sync_seed):
    _seed(session, sync_seed)
    ctx = sync_seed.context
    camps = [
        r
        for r in build_rows(session, context=ctx, bc_id="bc-a", filters=_filters("campaign"))
        if r.display["advertiser_id"] == "acc-1"
    ]
    acct = next(
        r
        for r in build_rows(session, context=ctx, bc_id="bc-a", filters=_filters("account"))
        if r.display["remote_id"] == "acc-1"
    )
    for key in ("spend", "impressions", "clicks", "revenue_d0", "revenue_total"):
        assert acct.metric_buckets[0].values[key] == sum(
            c.metric_buckets[0].values[key] for c in camps
        )
    assert "account_status" in acct.display
