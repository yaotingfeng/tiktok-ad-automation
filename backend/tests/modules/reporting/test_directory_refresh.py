from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import refresh_report_directory
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.sync_conftest import sync_seed as sync_seed
from tests.modules.reporting.test_jobs import _route


def prepared(session, seed, *, day=None, spend="30", tier="hot", count=1, types=None):
    day = day or datetime.now(UTC).date()
    job_id = enqueue_job(
        session,
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        tier=tier,
        advertiser_ids=["acc-2"],
        start=day,
        end=day,
        frozen_route=_route(seed),
        actor_id=seed.actor_id,
    )
    rows = [
        ad_row(
            f"a{x}", day, adv="acc-2", campaign=f"c{x}", adgroup=f"g{x}", spend=spend
        )
        for x in range(count)
    ]
    if types:
        for row, kind in zip(rows, types, strict=True):
            row["metrics"]["campaign_automation_type"] = kind
    publish_ad_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=job_id,
        accounts={"acc-2": AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=rows,
    )
    return session.get(ReportSyncJob, job_id)


@pytest.mark.parametrize("case", ["zero", "history", "daily", "disabled"])
def test_only_live_spending_hot_report_needs_directory(
    session, sync_seed, monkeypatch, case
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", case != "disabled")
    job = prepared(
        session,
        sync_seed,
        spend="0" if case == "zero" else "30",
        day=datetime.now(UTC).date() - timedelta(days=10)
        if case == "history"
        else None,
        tier="daily" if case == "daily" else "hot",
    )
    assert refresh_report_directory(session, job) == 0


@pytest.mark.parametrize("stale", [False, True])
def test_fresh_parent_chain_skips_read_and_stale_chain_refreshes(
    session, sync_seed, monkeypatch, stale
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(session, sync_seed)
    for kind, remote in [("ad", "a0"), ("adgroup", "g0"), ("campaign", "c0")]:
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2",
                kind=kind,
                remote_id=remote,
                ad_type="SMART_PLUS",
                observed_at=datetime.now(UTC) - timedelta(hours=2 if stale else 0),
                published_version=1,
            )
        )
    session.flush()
    assert refresh_report_directory(session, job) == (3 if stale else 0)


def test_report_refresh_respects_limit_and_ad_type(session, sync_seed, monkeypatch):
    from app.modules.reporting import directory_refresh

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(directory_refresh, "MAX_REFRESH_ADS", 2)
    job = prepared(
        session,
        sync_seed,
        count=3,
        types=["UPGRADED_SMART_PLUS", "MANUAL", "UPGRADED_SMART_PLUS"],
    )
    assert refresh_report_directory(session, job) == 6
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    ads = {r.ad_type: r.query["ids"] for r in runs if r.kind == "ad"}
    assert ads == {"SMART_PLUS": ["a0"], "REGULAR": ["a1"]}


@pytest.mark.parametrize(
    "mode,selected_bc,expected",
    [("BC", "bc-a", 3), ("BC", "bc-b", 0), ("ALL", None, 3), ("ACCOUNTS", None, 3)],
)
def test_existing_directory_refresh_prioritizes_exact_rule_scope(
    session, sync_seed, monkeypatch, mode, selected_bc, expected
):
    from app.modules.automation_rules.models import AutomationRule

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(session, sync_seed)
    for kind, remote in [("ad", "a0"), ("adgroup", "g0"), ("campaign", "c0")]:
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2",
                kind=kind,
                remote_id=remote,
                ad_type="SMART_PLUS",
                observed_at=now - timedelta(minutes=10),
                published_version=1,
            )
        )
    session.add(
        AutomationRule(
            tenant_id=sync_seed.tenant_id,
            name="scope-test",
            created_by=sync_seed.actor_id,
            updated_by=sync_seed.actor_id,
            definition={
                "accounts": {
                    "mode": mode,
                    "bc_ids": [selected_bc] if selected_bc else [],
                    "advertiser_ids": ["acc-2"],
                }
            },
        )
    )
    session.flush()
    assert refresh_report_directory(session, job, now=now) == expected


def test_pending_top_spenders_cannot_hide_new_missing_ad(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting import directory_refresh

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(session, sync_seed, count=101)
    monkeypatch.setattr(directory_refresh, "MAX_REFRESH_ADS", 100)
    assert refresh_report_directory(session, job) == 3
    # 原100个ID仍在途，下一轮必须能选到第101个，不能先LIMIT后去重。
    assert refresh_report_directory(session, job) == 3
    runs = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == sync_seed.tenant_id, AdDirectoryRun.kind == "ad"
        )
    ).all()
    assert len(runs) == 2
    assert sum(len(run.query["ids"]) for run in runs) == 101
