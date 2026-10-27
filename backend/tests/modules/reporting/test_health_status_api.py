"""新鲜度更新不能掩盖旧覆盖结果；接口输出真实独立时间。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.modules.reporting import health, planner
from app.modules.reporting.store_models import AdSyncBc, ReportSyncHealth
from tests.modules.reporting.test_sync_api import sync_api as sync_api


@pytest.mark.parametrize(
    "status,age,issue",
    [
        (None, 0, "COVERAGE_PENDING"),
        ("COMPLETE", 301, "COVERAGE_STALE"),
        ("FAILED", 30, "COVERAGE_QUERY_FAILED"),
        ("COMPLETE", 30, None),
    ],
)
def test_health_api_keeps_coverage_age_separate(
    sync_api, session, sync_seed, monkeypatch, status, age, issue
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    at = now - timedelta(seconds=age)
    metrics = (
        {}
        if status is None
        else {
            "coverage_status": status,
            "coverage_observed_at": at.isoformat(),
            "coverage_completed_at": (at + timedelta(seconds=1)).isoformat(),
            "coverage_scope": health._health_scope(
                session, plan, planner.owned_accounts(session, plan), now=now
            ),
        }
    )
    session.add(
        ReportSyncHealth(
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            status="HEALTHY",
            metrics=metrics,
            issues=[],
            observed_at=now,
        )
    )
    session.flush()
    client, path = sync_api
    response = client.get(
        f"{path}/ad-sync-status",
        params={
            "bc_id": "bc-a",
            "start_date": now.date().isoformat(),
            "end_date": now.date().isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    value = response.json()["health"]
    assert value["status"] == ("DEGRADED" if issue else "HEALTHY")
    if issue:
        assert issue in value["issues"]
    assert value["coverage_observed_at"] == (
        at.isoformat().replace("+00:00", "Z") if status else None
    )
    assert "coverage_scope" not in value


def test_health_api_invalidates_coverage_immediately_after_timezone_change(
    sync_api, session, sync_seed, monkeypatch
):
    from app.modules.accounts.models import AdvertiserAccount

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    owned = planner.owned_accounts(session, plan)
    scope = health._health_scope(session, plan, owned, now=now)
    session.add(
        ReportSyncHealth(
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            status="HEALTHY",
            observed_at=now,
            issues=[],
            metrics={
                "coverage_status": "COMPLETE",
                "coverage_scope": scope,
                "coverage_observed_at": now.isoformat(),
                "coverage_completed_at": now.isoformat(),
            },
        )
    )
    account = session.get(AdvertiserAccount, (sync_seed.tenant_id, owned[0]))
    account.timezone = "UTC" if account.timezone != "UTC" else "Asia/Hong_Kong"
    session.flush()
    client, path = sync_api
    response = client.get(
        f"{path}/ad-sync-status",
        params={
            "bc_id": "bc-a",
            "start_date": now.date().isoformat(),
            "end_date": now.date().isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    value = response.json()["health"]
    assert value["status"] == "DEGRADED" and "COVERAGE_PENDING" in value["issues"]
