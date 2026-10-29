"""目录晚账户耗尽预算，已成功账户及游标仍须一起提交。"""

from datetime import UTC, datetime, timedelta
from time import monotonic

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.core.db import engine
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks, planner
from app.modules.reporting import planning_steps as steps
from app.modules.reporting.store_models import AdSyncAccount, AdSyncBc


def _seed_directory(session, seed, now, count=64):
    planner.ensure_bc_plans(session, now=now)
    accounts = [f"cooperate-{i:03}" for i in range(count)]
    session.add_all(
        [
            AdvertiserAccount(
                tenant_id=seed.tenant_id,
                advertiser_id=a,
                timezone="UTC",
                currency="USD",
            )
            for a in accounts
        ]
    )
    session.flush()
    for a in accounts:
        session.add(
            BCAccountAccess(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                advertiser_id=a,
                connection_id=seed.connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=now,
            )
        )
        session.add(
            AdSyncAccount(tenant_id=seed.tenant_id, advertiser_id=a, owner_bc_id="bc-a")
        )
    session.flush()
    plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
    plan.next_directory_at = now
    plan.next_hot_at = plan.next_material_at = plan.next_daily_at = now + timedelta(
        days=1
    )
    owned = planner.owned_accounts(session, plan)
    state = steps._prepare(
        plan, owned, now, planner._timezones(session, seed.tenant_id, owned)
    )
    state["phase"] = steps.PHASE_ORDER.index("directory")
    plan.screening_progress = {steps.KEY: state}
    return accounts


def _directory_turn(factory, seed, now, redis_client, budget=5):
    # 实际阶段入口与两个嵌套预算；每轮重新读取持久游标，不能手工推进after。
    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        state = dict(plan.screening_progress[steps.KEY])
        state["phase"] = steps.PHASE_ORDER.index("directory")
        state.pop("directory_retry", None)
        plan.screening_progress = {**plan.screening_progress, steps.KEY: state}
    steps.plan_step(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        now=now,
        allow_backfill=False,
        budget=budget,
    )


def test_two_full_batches_keep_committed_prefix_instead_of_restarting_64(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    with factory() as session, session.begin():
        accounts = _seed_directory(session, seed, now)
    original = job_tasks.request_directory
    calls = []

    def timed_request(session, **kwargs):
        calls.append(tuple(kwargs["advertiser_ids"]))
        # 真实PG延迟按账户累积，旧64户同保存点约需3.84s，外层3s整批回滚。
        for _ in kwargs["advertiser_ids"]:
            session.execute(text("SELECT pg_sleep(0.06)"))
        return original(session, **kwargs)

    monkeypatch.setattr(job_tasks, "request_directory", timed_request)
    execute = steps._execute

    def finishing_cost(session, *args):
        result = execute(session, *args)
        # 目录处理后仍需保存阶段/游标；真实PG尾成本复现内层预算结束后的再次取消。
        session.execute(text("SELECT pg_sleep(0.1)"))
        return result

    monkeypatch.setattr(steps, "_execute", finishing_cost)
    samples = []
    for turn in range(2):
        started = monotonic()
        _directory_turn(factory, seed, now + timedelta(seconds=30 * turn), redis_client)
        assert monotonic() - started < 3.5
        with factory() as session:
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            runs = session.exec(
                select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
            ).all()
            completed = {r.advertiser_id for r in runs} & set(accounts)
            cursor = plan.screening_progress.get(job_tasks.DIRECTORY_PLAN_PROGRESS, {})
            samples.append((completed, cursor.get("after", "")))
    assert samples[0][0], (samples, calls)
    assert samples[0][0] < samples[1][0], (samples, calls)
    assert samples[1][1] > samples[0][1], samples


def test_late_account_timeout_keeps_prefix_then_full_timeout_defers_only_that_account(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    with factory() as session, session.begin():
        accounts = _seed_directory(session, seed, now, count=6)
    original = job_tasks.request_directory
    slow = accounts[2]

    def timed_request(session, **kwargs):
        if slow in kwargs["advertiser_ids"]:
            result = original(session, **kwargs)
            # 此户已写出任务后再取消，SAVEPOINT须撤销本户，不能留下半完成任务。
            session.execute(text("SELECT pg_sleep(4)"))
            return result
        session.execute(text("SELECT pg_sleep(0.06)"))
        return original(session, **kwargs)

    monkeypatch.setattr(job_tasks, "request_directory", timed_request)
    for turn in range(3):
        _directory_turn(factory, seed, now + timedelta(seconds=30 * turn), redis_client)
        with factory() as session:
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            runs = session.exec(
                select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
            ).all()
            created = {r.advertiser_id for r in runs}
            cursor = plan.screening_progress[job_tasks.DIRECTORY_PLAN_PROGRESS]
            assert set(accounts[:2]) <= created
            assert slow not in created
            assert len(runs) == len({(r.advertiser_id, r.partition_key) for r in runs})
            if turn == 0:
                assert slow not in cursor["deferred"]  # 尾片不足不误罚五分钟。
                assert cursor["after"] == accounts[1]
            else:
                assert slow in cursor["deferred"]
            if turn == 2:
                assert set(accounts) - {slow} <= created
    monkeypatch.setattr(job_tasks, "request_directory", original)
    _directory_turn(factory, seed, now + timedelta(minutes=6), redis_client)
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert job_tasks.DIRECTORY_PLAN_PROGRESS not in plan.screening_progress
        runs = session.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
        ).all()
        assert set(accounts) <= {r.advertiser_id for r in runs}
        assert len(runs) == len({(r.advertiser_id, r.partition_key) for r in runs})


@pytest.mark.parametrize("code", ["40P01", "55P03"])
def test_account_lock_conflict_keeps_earlier_success_and_short_retry(
    monkeypatch, committed_sync_seed, redis_client, code
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    with factory() as session, session.begin():
        accounts = _seed_directory(session, seed, now, count=3)
    original = job_tasks.request_directory

    def conflict(session, **kwargs):
        if accounts[1] in kwargs["advertiser_ids"]:
            session.execute(
                text(
                    "DO $$ BEGIN RAISE EXCEPTION 'test conflict' USING ERRCODE = '"
                    + code
                    + "'; END $$"
                )
            )
        return original(session, **kwargs)

    monkeypatch.setattr(job_tasks, "request_directory", conflict)
    _directory_turn(factory, seed, now, redis_client)
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        cursor = plan.screening_progress[job_tasks.DIRECTORY_PLAN_PROGRESS]
        assert cursor["batch_size"] == 64
        assert cursor["after"] == accounts[2]
        assert datetime.fromisoformat(
            cursor["deferred"][accounts[1]]
        ) == now + timedelta(seconds=5)
        created = set(
            session.exec(
                select(AdDirectoryRun.advertiser_id).where(
                    AdDirectoryRun.tenant_id == seed.tenant_id
                )
            ).all()
        )
        assert accounts[0] in created and accounts[2] in created
        assert accounts[1] not in created


def test_tail_without_work_budget_leaves_all_accounts_pending(
    monkeypatch, committed_sync_seed
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    with factory() as session, session.begin():
        accounts = _seed_directory(session, seed, now, count=3)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        job_tasks._plan_directory_step(
            session, plan, accounts=accounts, now=now, budget=0.2
        )
        session.flush()
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        cursor = plan.screening_progress[job_tasks.DIRECTORY_PLAN_PROGRESS]
        assert cursor["after"] == ""
        assert not cursor["deferred"]
        assert cursor["batch_size"] == 64
        assert plan.next_directory_at < now + planner.DIRECTORY_INTERVAL
        assert not session.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
        ).all()


def test_short_entry_slice_does_not_penalize_first_account_that_fits_full_slice(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    with factory() as session, session.begin():
        _seed_directory(session, seed, now, count=1)
    original = job_tasks.request_directory

    def slow_first(session, **kwargs):
        if "acc-1" in kwargs["advertiser_ids"]:
            session.execute(text("SELECT pg_sleep(2)"))
        return original(session, **kwargs)

    monkeypatch.setattr(job_tasks, "request_directory", slow_first)
    _directory_turn(factory, seed, now, redis_client, budget=2)
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        cursor = plan.screening_progress[job_tasks.DIRECTORY_PLAN_PROGRESS]
        assert cursor["after"] == ""
        assert not cursor["deferred"]
    _directory_turn(factory, seed, now + timedelta(seconds=30), redis_client, budget=3)
    with factory() as session:
        runs = session.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
        ).all()
        assert "acc-1" in {run.advertiser_id for run in runs}
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert "acc-1" not in plan.screening_progress.get(
            job_tasks.DIRECTORY_PLAN_PROGRESS, {}
        ).get("deferred", {})
