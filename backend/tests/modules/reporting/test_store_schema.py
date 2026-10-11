# backend/tests/modules/reporting/test_store_schema.py
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text

from app.modules.reporting.partitions import ensure_month_partitions
from app.modules.reporting.store_models import AdReportDay, ReportSyncJob


def test_ad_report_day_is_partitioned_and_accepts_rows(session):
    ensure_month_partitions(session, today=date(2026, 10, 12))
    parts = (
        session.execute(
            text(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
                "JOIN pg_class p ON p.oid=i.inhparent WHERE p.relname='ad_report_day'"
            )
        )
        .scalars()
        .all()
    )
    assert "ad_report_day_2026_10" in parts and "ad_report_day_2027_01" in parts
    assert "ad_report_day_default" in parts
    session.add(
        AdReportDay(
            tenant_id=uuid4(),
            advertiser_id="a1",
            stat_date=date(2026, 10, 5),
            ad_id="ad1",
            campaign_id="c1",
            adgroup_id="g1",
            currency="USD",
            timezone="UTC",
            spend=Decimal("1.23"),
            revenue_d0=None,
            fetched_at=datetime.now(UTC),
            sync_job_id=uuid4(),
        )
    )
    session.flush()


def test_ensure_month_partitions_is_idempotent(session):
    ensure_month_partitions(session, today=date(2026, 10, 12))
    ensure_month_partitions(session, today=date(2026, 10, 12))


def test_job_status_check_rejects_unknown(session):
    import pytest
    from sqlalchemy.exc import IntegrityError

    session.add(
        ReportSyncJob(
            tenant_id=uuid4(),
            bc_id="bc",
            kind="ad_day",
            priority=1,
            status="BOGUS",
            advertiser_ids=["a"],
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 1),
            job_key="k",
            frozen_route={},
            actor_id=uuid4(),
            due_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
