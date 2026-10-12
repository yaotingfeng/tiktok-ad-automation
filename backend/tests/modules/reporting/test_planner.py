from datetime import UTC, date, datetime, timedelta

from sqlmodel import select

from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting.planner import (
    account_owners,
    batch_accounts,
    ensure_bc_plans,
    plan_daily,
    plan_history,
    plan_hot,
    screen_active,
)
from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob

NOW = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)


def test_auth_creates_plans_without_manual_refresh(session, sync_seed):
    assert ensure_bc_plans(session, now=NOW) == 2
    plans = session.exec(
        select(AdSyncBc).where(AdSyncBc.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {p.bc_id for p in plans} == {"bc-a", "bc-b"} and all(
        p.next_hot_at <= NOW and p.next_daily_at <= NOW for p in plans
    )


def test_connection_level_discovery_creates_official_api_plans(session, sync_seed):
    """Official API discovery is connection-wide and stores no BC scope."""
    from app.modules.accounts.models import DiscoveryRun

    for run in session.exec(
        select(DiscoveryRun).where(
            DiscoveryRun.tenant_id == sync_seed.tenant_id,
            DiscoveryRun.connection_id == sync_seed.connection.id,
        )
    ).all():
        run.bc_id = None
    session.flush()

    assert ensure_bc_plans(session, now=NOW) == 2
    plans = session.exec(
        select(AdSyncBc).where(AdSyncBc.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {p.bc_id for p in plans} == {"bc-a", "bc-b"}
    assert all(p.actor_id == sync_seed.actor_id for p in plans)


def test_shared_account_is_planned_once(session, sync_seed):
    ensure_bc_plans(session, now=NOW)
    owners = account_owners(session, tenant_id=sync_seed.tenant_id)
    assert owners == {
        "acc-1": "bc-a",
        "acc-2": "bc-a",
    }  # 按 bc_id 字典序选主 BC，稳定不抖动


def test_mcp_only_bc_is_not_planned(session, sync_seed):
    from app.modules.accounts.connection_models import BCConnectionBinding

    binding = session.get(
        BCConnectionBinding, (sync_seed.tenant_id, "bc-b", sync_seed.connection.id)
    )
    from app.modules.accounts.models import TikTokConnection

    mcp = TikTokConnection(
        tenant_id=sync_seed.tenant_id, kind="OFFICIAL_MCP", status="ACTIVE"
    )
    session.add(mcp)
    session.flush()
    binding.connection_id = mcp.id
    binding.kind = "OFFICIAL_MCP"  # 实际外键要求连接与绑定的通道一致
    session.flush()
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-b"))
    assert plan is None or (
        plan.enabled is False and plan.disabled_reason == "api_binding_required"
    )


def test_screen_uses_bc_report_and_filters_candidates():
    class R:
        def bc_spend(self, *, bc_id, start, end, page):
            rows = (
                {
                    "dimensions": {"advertiser_id": "acc-1"},
                    "metrics": {"spend": "3.10"},
                },
                {
                    "dimensions": {"advertiser_id": "acc-9"},
                    "metrics": {"spend": "1.00"},
                },
                {
                    "dimensions": {"advertiser_id": "acc-2"},
                    "metrics": {"spend": "0.000"},
                },
            )
            return RawReportPage(rows, 1, 1, 3)

    assert screen_active(
        R(),
        bc_id="bc-a",
        start=date(2026, 10, 12),
        end=date(2026, 10, 12),
        candidates={"acc-1", "acc-2"},
    ) == {"acc-1"}


def test_hot_plan_jobs_and_utc_noon_rule(session, sync_seed):
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan_hot(session, plan, active=["acc-1", "acc-2"], now=NOW)
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    ad_jobs = [j for j in jobs if j.kind == "ad_day"]
    # UTC 12:00 前：香港账户今天 10-12 与昨天 10-11 都要刷；hour 任务今天、昨天各一个
    assert {(j.start_date, j.end_date) for j in ad_jobs} >= {
        (date(2026, 10, 11), date(2026, 10, 12))
    }
    assert {j.kind for j in jobs} == {"ad_day", "campaign_hour"}
    assert all(j.priority == 1 for j in jobs)


def test_inactive_account_keeps_published_rows_until_daily_refresh(session, sync_seed):
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan_hot(session, plan, active=[], now=NOW)
    assert (
        session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
        ).all()
        == []
    )
    plan_daily(session, plan, active_32d=["acc-1"], now=NOW.replace(hour=13))
    kinds = {
        j.kind
        for j in session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
        ).all()
    }
    assert kinds == {"ad_day", "material_day", "campaign_day_utc", "account_day"}


def test_history_shards_30_days_from_account_creation(session, sync_seed):
    from app.modules.accounts.models import AdvertiserAccount

    acc = session.get(AdvertiserAccount, (sync_seed.tenant_id, "acc-1"))
    acc.remote_created_at = datetime(2026, 7, 1, tzinfo=UTC)
    session.flush()
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan_history(session, plan, now=NOW)
    ad = sorted(
        (j.start_date, j.end_date)
        for j in session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == sync_seed.tenant_id,
                ReportSyncJob.kind == "ad_day",
            )
        ).all()
    )
    assert ad[0][0] == date(2026, 7, 1) and all((e - s).days < 30 for s, e in ad)
    assert ad[-1][1] < date(2026, 10, 12) - timedelta(days=31)
    plan_history(session, plan, now=NOW)  # 第二次不重复规划
    assert len(
        session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.kind == "ad_day",
                ReportSyncJob.tenant_id == sync_seed.tenant_id,
            )
        ).all()
    ) == len(ad)


def test_batch_accounts_shrinks_for_large_accounts(session, sync_seed):
    assert batch_accounts(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_ids=["acc-1", "acc-2"],
        days=1,
    ) == [["acc-1", "acc-2"]]


def test_ten_thousand_accounts_one_round_call_budget():
    """规模估算：1 万账户、2000 活跃、50 个 BC 的一轮热刷请求数在规格范围内。"""
    from app.modules.reporting.planner import estimate_round_requests

    assert (
        estimate_round_requests(
            bcs=50, active=2000, batch=5, hour_pages=1, ad_pages=1.5
        )
        <= 2200
    )


def test_after_utc_noon_hours_keep_yesterday_but_days_only_today(session, sync_seed):
    from app.modules.reporting.planner import plan_material

    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan_hot(session, plan, active=["acc-1", "acc-2"], now=NOW.replace(hour=13))
    plan_material(session, plan, active=["acc-1", "acc-2"], now=NOW.replace(hour=13))
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    for kind in ("ad_day", "material_day"):
        assert {(j.start_date, j.end_date) for j in jobs if j.kind == kind} == {
            (date(2026, 10, 12), date(2026, 10, 12))
        }
    assert {(j.start_date, j.end_date) for j in jobs if j.kind == "campaign_hour"} == {
        (date(2026, 10, 11), date(2026, 10, 11)),
        (date(2026, 10, 12), date(2026, 10, 12)),
    }


def test_removed_binding_disables_existing_plan(session, sync_seed):
    from app.modules.accounts.connection_models import BCConnectionBinding

    ensure_bc_plans(session, now=NOW)
    binding = session.get(
        BCConnectionBinding, (sync_seed.tenant_id, "bc-a", sync_seed.connection.id)
    )
    session.delete(binding)
    session.flush()
    ensure_bc_plans(session, now=NOW)
    assert session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a")).enabled is False
    assert "acc-1" not in account_owners(session, tenant_id=sync_seed.tenant_id)
