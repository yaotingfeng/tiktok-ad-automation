"""已知活跃户免重复事实探针，其余归属户仍按当前日事实存在判定。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import insert

from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting import health
from app.modules.reporting.store_models import AdReportDay, MaterialReportDay


@pytest.mark.parametrize("model", [AdReportDay, MaterialReportDay])
def test_known_scope_keeps_zero_spend_inactive_facts_and_excludes_foreign_active(
    session, sync_seed, monkeypatch, model
):
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    owned = ["active-no-facts", "inactive-zero", "inactive-spend", "yesterday", "empty"]
    session.execute(
        insert(AdvertiserAccount),
        [
            {
                "tenant_id": sync_seed.tenant_id,
                "advertiser_id": account,
                "timezone": "UTC",
            }
            for account in owned + ["foreign-facts"]
        ],
    )
    session.execute(
        insert(model),
        [
            {
                "tenant_id": sync_seed.tenant_id,
                "advertiser_id": account,
                "stat_date": now.date() - timedelta(days=account == "yesterday"),
                "currency": "USD",
                "timezone": "UTC",
                "fetched_at": now,
                "sync_job_id": uuid4(),
                "spend": spend,
                **(
                    {"ad_id": "ad", "campaign_id": "c", "adgroup_id": "g"}
                    if model is AdReportDay
                    else {"main_material_id": "post", "main_material_type": "VIDEO"}
                ),
            }
            for account, spend in [
                ("inactive-zero", 0),
                ("inactive-spend", 5),
                ("yesterday", 1),
                ("foreign-facts", 1),
            ]
        ],
    )
    plan = SimpleNamespace(
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        last_active_advertiser_ids=["active-no-facts", "foreign-active"],
        active_screened_at=now,
    )
    original = health.known_reporting_accounts
    expected_active = (
        set(plan.last_active_advertiser_ids)
        | original(session, plan, accounts=owned, now=now)
    ) & set(owned)
    calls = []

    def known(db, current_plan, *, accounts, now):
        calls.append((list(accounts), now))
        return original(db, current_plan, accounts=accounts, now=now)

    monkeypatch.setattr(health, "known_reporting_accounts", known)
    monkeypatch.setattr(health, "FRESHNESS_BATCH_SIZE", 2)
    metrics, _, repairs = health.measure_freshness(
        session, plan, now=now, accounts=owned
    )
    assert expected_active == {"active-no-facts", "inactive-zero", "inactive-spend"}
    assert metrics["stale_ad_accounts"] == metrics["stale_material_accounts"] == 3
    assert repairs["ad"] == repairs["material"] == expected_active
    assert {account for accounts, _ in calls for account in accounts} == set(owned) - {
        "active-no-facts"
    }
    assert len(calls) == 2 and all(
        len(accounts) <= 2 and at == now for accounts, at in calls
    )
