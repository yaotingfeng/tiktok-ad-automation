from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlmodel import select

from app.core.db import engine
from app.integrations.tiktok.contracts.ads import AccountBalance
from app.integrations.tiktok.contracts.common import CallEvidence
from app.modules.reporting import balance_tasks, planner
from app.modules.reporting.dimension_query import build_rows
from app.modules.reporting.models import AccountBalanceObservation
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.store_models import AdSyncBc, ReportBalanceRun
from tests.modules.reporting.test_jobs import D, _route


def create_run(seed, factory, advertisers):
    with factory() as session:
        run = ReportBalanceRun(
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            run_key="balance-test",
            frozen_route=_route(seed),
            remaining_advertiser_ids=advertisers,
        )
        session.add(run)
        session.commit()
        return run.id


def test_balance_refresh_is_independent_and_visible(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    run_id = create_run(seed, factory, ["acc-1"])
    observed = datetime.now(UTC)

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(
            ads=SimpleNamespace(
                read_balance_page=lambda advs, page: (
                    {
                        advs[0]: AccountBalance(
                            Decimal("12.50"),
                            "USD",
                            "AVAILABLE",
                            observed,
                            CallEvidence(),
                            "ADVERTISER",
                            advs[0],
                        )
                    },
                    None,
                )
            )
        )

    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", gateway)
    assert (
        balance_tasks.run_balance(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=run_id,
            page=1,
        )
        == "COMPLETE"
    )
    with factory() as s:
        assert s.exec(
            select(AccountBalanceObservation).where(
                AccountBalanceObservation.tenant_id == seed.tenant_id
            )
        ).one().amount == Decimal("12.50")
        rows = build_rows(
            s,
            context=seed.context,
            bc_id="bc-a",
            filters=ReportingFilter(dimension="account", start_date=D, end_date=D),
        )
        row = next(r for r in rows if r.display["advertiser_id"] == "acc-1")
        assert row.display["balance"] == "12.50"
        assert row.display["balance_currency"] == "USD"
        assert row.display["balance_scope"] == "ADVERTISER"
        assert row.metric_buckets[0].values["spend"] is None


def test_balance_plan_coalesces_per_half_hour(session, sync_seed, monkeypatch):
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    assert balance_tasks.enqueue_balances(session, plan, ["acc-1"], now) == 1
    assert balance_tasks.enqueue_balances(session, plan, ["acc-1"], now) == 0


def test_disabled_balance_never_opens_gateway(monkeypatch, sync_seed):
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", False)
    assert (
        balance_tasks.run_balance(
            database_engine=None,
            redis_client=None,
            context=sync_seed.context,
            run_id=None,
            page=1,
        )
        == "WAITING"
    )


def test_balance_schedule_ignores_report_backoff_and_coalesces(
    monkeypatch, committed_sync_seed
):
    from datetime import timedelta

    from app.jobs.celery_app import celery_app
    from app.jobs.models import PendingDispatch

    seed, factory = committed_sync_seed
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime(2026, 10, 12, 13, tzinfo=UTC)
    with factory() as s:
        planner.ensure_bc_plans(s, now=now)
        for plan in s.exec(
            select(AdSyncBc).where(AdSyncBc.tenant_id == seed.tenant_id)
        ):
            plan.next_hot_at = plan.next_daily_at = plan.next_material_at = (
                plan.next_directory_at
            ) = now + timedelta(days=1)
        s.commit()
    assert balance_tasks.plan_balances(database_engine=engine, now=now) == 1
    assert balance_tasks.plan_balances(database_engine=engine, now=now) == 0
    assert (
        balance_tasks.plan_balances(
            database_engine=engine, now=now + timedelta(minutes=30)
        )
        == 1
    )
    with factory() as s:
        rows = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == balance_tasks.TASK_NAME,
            )
        ).all()
        assert len(rows) == 2
    entries = [
        e
        for e in celery_app.conf.beat_schedule.values()
        if e["task"] == "reporting.plan_balances"
    ]
    assert len(entries) == 1 and entries[0]["schedule"] == 1800


def test_balance_page_commits_observations_with_next_page(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.jobs.models import PendingDispatch

    seed, factory = committed_sync_seed
    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    calls = []

    @contextmanager
    def gateway(**_kwargs):
        def read(advs, *, page):
            calls.append((advs, page))
            adv = "acc-1" if page == 1 else "acc-2"
            return {
                adv: AccountBalance(
                    Decimal("8"),
                    "USD",
                    "AVAILABLE",
                    datetime.now(UTC),
                    CallEvidence(),
                    "ADVERTISER",
                    adv,
                )
            }, 2 if page == 1 else None

        yield SimpleNamespace(ads=SimpleNamespace(read_balance_page=read))

    monkeypatch.setattr(balance_tasks, "open_tiktok_gateway", gateway)
    args = {
        "database_engine": engine,
        "redis_client": redis_client,
        "context": seed.context,
        "run_id": create_run(seed, factory, ["acc-1", "acc-2"]),
    }
    assert balance_tasks.run_balance(**args, page=1) == "CONTINUE"
    with factory() as s:
        pending = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key == "balance-test:page:2",
            )
        ).one()
        payload = pending.payload
        assert payload["page"] == 2 and payload["run_id"] == str(args["run_id"])
        assert s.get(ReportBalanceRun, args["run_id"]).remaining_advertiser_ids == [
            "acc-2"
        ]
        assert (
            s.exec(
                select(AccountBalanceObservation).where(
                    AccountBalanceObservation.tenant_id == seed.tenant_id
                )
            )
            .one()
            .advertiser_id
            == "acc-1"
        )
    assert balance_tasks.run_balance(**args, page=payload["page"]) == "COMPLETE"
    assert balance_tasks.run_balance(**args, page=1) == "IGNORED"
    assert calls == [(("acc-1", "acc-2"), 1), (("acc-2",), 2)]


def test_large_balance_scope_stays_out_of_bounded_queue_payload(
    session, sync_seed, monkeypatch
):
    import json

    from app.jobs.models import PendingDispatch

    monkeypatch.setattr(balance_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    accounts = [str(7000000000000000000 + i) for i in range(2000)]
    assert balance_tasks.enqueue_balances(session, plan, accounts, now) == 1
    pending = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
            PendingDispatch.task_name == balance_tasks.TASK_NAME,
        )
    ).one()
    assert len(json.dumps(pending.payload)) < 200
    assert set(pending.payload) == {"run_id", "page"}
    from uuid import UUID

    assert (
        session.get(
            ReportBalanceRun, UUID(pending.payload["run_id"])
        ).remaining_advertiser_ids
        == accounts
    )
