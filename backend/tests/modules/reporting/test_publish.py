# backend/tests/modules/reporting/test_publish.py
from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlmodel import select

from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.store_models import (
    AdReportDay,
    CampaignReportDay,
    ReportSyncState,
)
from tests.modules.reporting.sync_conftest import ad_row

HK = {"acc-1": AccountMeta("USD", "Asia/Hong_Kong")}
D = date(2026, 10, 5)


def test_publish_replaces_range_and_rolls_up_campaigns(session, sync_seed):
    t = sync_seed.tenant_id
    publish_ad_day(
        session,
        tenant_id=t,
        job_id=uuid4(),
        accounts=HK,
        start=D,
        end=D,
        rows=[ad_row("a1", D), ad_row("a2", D, spend="2.00")],
    )
    publish_ad_day(
        session,
        tenant_id=t,
        job_id=uuid4(),
        accounts=HK,
        start=D,
        end=D,
        rows=[ad_row("a1", D, spend="5.00")],
    )
    ads = session.exec(select(AdReportDay).where(AdReportDay.tenant_id == t)).all()
    assert [(r.ad_id, r.spend) for r in ads] == [("a1", Decimal("5.00"))]
    camp = session.exec(
        select(CampaignReportDay).where(CampaignReportDay.tenant_id == t)
    ).one()
    assert camp.spend == Decimal("5.00") and camp.campaign_name == "嘉书-剧c1-备注"
    state = session.get(ReportSyncState, (t, "acc-1", "ad_day", D))
    assert state.status == "COMPLETE" and state.row_count == 1


def test_empty_result_deletes_and_marks_complete(session, sync_seed):
    t = sync_seed.tenant_id
    publish_ad_day(
        session,
        tenant_id=t,
        job_id=uuid4(),
        accounts=HK,
        start=D,
        end=D,
        rows=[ad_row("a1", D)],
    )
    publish_ad_day(
        session, tenant_id=t, job_id=uuid4(), accounts=HK, start=D, end=D, rows=[]
    )
    assert (
        session.exec(select(AdReportDay).where(AdReportDay.tenant_id == t)).all() == []
    )
    assert session.get(ReportSyncState, (t, "acc-1", "ad_day", D)).status == "COMPLETE"


def test_replace_only_touches_requested_accounts_and_dates(session, sync_seed):
    t = sync_seed.tenant_id
    both = {
        "acc-1": AccountMeta("USD", "Asia/Hong_Kong"),
        "acc-2": AccountMeta("USD", "UTC"),
    }
    publish_ad_day(
        session,
        tenant_id=t,
        job_id=uuid4(),
        accounts=both,
        start=D,
        end=D,
        rows=[ad_row("a1", D), ad_row("b1", D, adv="acc-2")],
    )
    other_day = date(2026, 10, 4)
    publish_ad_day(
        session,
        tenant_id=t,
        job_id=uuid4(),
        accounts=HK,
        start=other_day,
        end=other_day,
        rows=[ad_row("a9", other_day)],
    )
    publish_ad_day(
        session, tenant_id=t, job_id=uuid4(), accounts=HK, start=D, end=D, rows=[]
    )
    left = {
        (r.advertiser_id, r.stat_date, r.ad_id)
        for r in session.exec(
            select(AdReportDay).where(AdReportDay.tenant_id == t)
        ).all()
    }
    assert left == {("acc-2", D, "b1"), ("acc-1", other_day, "a9")}


def test_null_member_makes_rollup_null(session, sync_seed):
    t = sync_seed.tenant_id
    publish_ad_day(
        session,
        tenant_id=t,
        job_id=uuid4(),
        accounts=HK,
        start=D,
        end=D,
        rows=[ad_row("a1", D, total="-"), ad_row("a2", D)],
    )
    camp = session.exec(
        select(CampaignReportDay).where(CampaignReportDay.tenant_id == t)
    ).one()
    assert camp.revenue_total is None and camp.spend == Decimal("2.00")


def test_row_outside_requested_range_is_rejected(session, sync_seed):
    import pytest

    from app.core.errors import DomainError

    with pytest.raises(DomainError):
        publish_ad_day(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts=HK,
            start=D,
            end=D,
            rows=[ad_row("a1", date(2026, 10, 6))],
        )


def test_partial_campaign_replace_keeps_other_campaigns(session, sync_seed):
    t = sync_seed.tenant_id
    publish_ad_day(
        session,
        tenant_id=t,
        job_id=uuid4(),
        accounts=HK,
        start=D,
        end=D,
        rows=[ad_row("a1", D, campaign="c1"), ad_row("a2", D, campaign="c2")],
    )
    publish_ad_day(
        session,
        tenant_id=t,
        job_id=uuid4(),
        accounts=HK,
        start=D,
        end=D,
        rows=[ad_row("a1", D, campaign="c1", spend="9.00")],
        campaign_ids=("c1",),
    )
    camps = {
        r.campaign_id: r.spend
        for r in session.exec(
            select(CampaignReportDay).where(CampaignReportDay.tenant_id == t)
        ).all()
    }
    assert camps == {"c1": Decimal("9.00"), "c2": Decimal("1.00")}


def test_hour_replace_uses_each_accounts_local_boundary(session, sync_seed):
    from datetime import UTC, datetime

    from app.modules.reporting.publish import publish_campaign_hour
    from app.modules.reporting.store_models import CampaignReportHour

    t = sync_seed.tenant_id
    both = HK | {"acc-2": AccountMeta("USD", "UTC")}

    def hour(adv):
        return {
            "dimensions": {
                "campaign_id": "c1",
                "stat_time_hour": "2026-10-05 10:00:00",
            },
            "metrics": {"advertiser_id": adv, "spend": "1"},
        }

    publish_campaign_hour(
        session,
        tenant_id=t,
        job_id=uuid4(),
        accounts=both,
        day=D,
        rows=[hour("acc-1"), hour("acc-2")],
    )
    assert (
        session.get(
            CampaignReportHour, (t, "acc-1", datetime(2026, 10, 5, 2, tzinfo=UTC), "c1")
        )
        is not None
    )
    publish_campaign_hour(
        session, tenant_id=t, job_id=uuid4(), accounts=HK, day=D, rows=[]
    )
    rows = session.exec(
        select(CampaignReportHour).where(CampaignReportHour.tenant_id == t)
    ).all()
    assert [(r.advertiser_id, r.stat_hour) for r in rows] == [
        ("acc-2", datetime(2026, 10, 5, 10, tzinfo=UTC))
    ]


def test_failed_refresh_keeps_rows_and_last_success(session, sync_seed):
    from app.modules.reporting.publish import mark_failed

    t = sync_seed.tenant_id
    publish_ad_day(
        session,
        tenant_id=t,
        job_id=uuid4(),
        accounts=HK,
        start=D,
        end=D,
        rows=[ad_row("a1", D)],
    )
    success = session.get(ReportSyncState, (t, "acc-1", "ad_day", D)).last_success_at
    mark_failed(
        session,
        tenant_id=t,
        advertiser_ids=["acc-1"],
        kind="ad_day",
        start=date(2026, 10, 4),
        end=D,
        error_code="read_error",
    )
    assert session.get(ReportSyncState, (t, "acc-1", "ad_day", D)).status == "STALE"
    assert (
        session.get(ReportSyncState, (t, "acc-1", "ad_day", D)).last_success_at
        == success
    )
    assert (
        session.get(ReportSyncState, (t, "acc-1", "ad_day", date(2026, 10, 4))).status
        == "FAILED"
    )
    assert (
        len(session.exec(select(AdReportDay).where(AdReportDay.tenant_id == t)).all())
        == 1
    )


def test_account_reconciliation_keeps_original_and_ad_sum(session, sync_seed):
    from app.modules.reporting.publish import publish_account_reconciliation
    from app.modules.reporting.store_models import AccountReconciliationDay

    t = sync_seed.tenant_id
    publish_ad_day(
        session,
        tenant_id=t,
        job_id=uuid4(),
        accounts=HK,
        start=D,
        end=D,
        rows=[ad_row("a1", D, spend="499.28", total="135.84")],
    )
    publish_account_reconciliation(
        session,
        tenant_id=t,
        accounts=HK,
        start=D,
        end=D,
        rows=[
            {
                "dimensions": {"advertiser_id": "acc-1", "stat_time_day": str(D)},
                "metrics": {
                    "spend": "499.29",
                    "native_growth_total_ad_impression_value": "135.85",
                },
            }
        ],
    )
    r = session.get(AccountReconciliationDay, (t, "acc-1", D))
    assert (
        r.account_spend,
        r.ad_sum_spend,
        r.account_revenue_total,
        r.ad_sum_revenue_total,
    ) == tuple(map(Decimal, ["499.29", "499.28", "135.85", "135.84"]))


def test_material_and_utc_daily_replace(session, sync_seed):
    from app.modules.reporting.publish import (
        publish_campaign_day_utc,
        publish_material_day,
    )
    from app.modules.reporting.store_models import (
        CampaignReportDayUtc,
        MaterialReportDay,
    )

    t = sync_seed.tenant_id
    args = {"tenant_id": t, "job_id": uuid4(), "accounts": HK, "start": D, "end": D}
    publish_material_day(
        session,
        **args,
        rows=[
            {
                "dimensions": {
                    "main_material_id": "m1",
                    "main_material_type": "VIDEO",
                    "stat_time_day": str(D),
                },
                "metrics": {
                    "spend": "1.234567890123456789",
                    "onsite_ad_impression_ad_revenue_day0": "0",
                    "main_material_name": "主素材",
                },
            }
        ],
    )
    row = session.get(MaterialReportDay, (t, "acc-1", D, "m1"))
    assert row.spend == Decimal("1.234567890123456789") and row.revenue_d0 == 0
    assert row.revenue_d3 is None and row.revenue_d30 is None
    publish_campaign_day_utc(
        session,
        **args,
        rows=[
            {
                "dimensions": {"campaign_id": "c1", "stat_time_day": str(D)},
                "metrics": {"spend": "1"},
            }
        ],
    )
    assert session.get(CampaignReportDayUtc, (t, "acc-1", D, "c1")).spend == 1
    publish_material_day(session, **args, rows=[])
    assert (
        session.exec(
            select(MaterialReportDay).where(MaterialReportDay.tenant_id == t)
        ).all()
        == []
    )


def test_hour_outside_requested_local_day_is_rejected(session, sync_seed):
    import pytest

    from app.core.errors import DomainError
    from app.modules.reporting.publish import publish_campaign_hour

    with pytest.raises(DomainError):
        publish_campaign_hour(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts=HK,
            day=D,
            rows=[
                {
                    "dimensions": {
                        "campaign_id": "c1",
                        "stat_time_hour": "2026-10-06 00:00:00",
                    },
                    "metrics": {"spend": "1"},
                }
            ],
        )


def test_campaign_daily_requires_real_identity(session, sync_seed):
    import pytest

    from app.core.errors import DomainError
    from app.modules.reporting.publish import publish_campaign_day_utc

    with pytest.raises(DomainError):
        publish_campaign_day_utc(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts=HK,
            start=D,
            end=D,
            rows=[{"dimensions": {"stat_time_day": str(D)}, "metrics": {"spend": "1"}}],
        )
