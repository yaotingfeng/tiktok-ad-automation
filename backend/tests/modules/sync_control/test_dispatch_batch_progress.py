"""真实PG/Redis验证预算回滚后的跨唤醒进展，不调用平台接口。"""

import os
from datetime import UTC, datetime

import pytest
from redis import Redis
from redis.exceptions import ConnectionError
from sqlalchemy import text
from sqlmodel import select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.reporting import job_tasks
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control import dispatcher
from app.modules.sync_control.models import SyncCostLedger, SyncSchedule
from app.modules.sync_control.occupancy import occupancy_keys
from app.modules.sync_control.schedule import report_endpoint
from tests.modules.sync_control.test_dispatch_batch_efficiency import seed_jobs


def test_hint_recovery_cannot_overwrite_a_newer_timeout(redis_client):
    key = f"sync:dispatch-batch:v1:{job_tasks.settings.TIKTOK_APP_ID}:report"
    assert job_tasks._dispatch_batch_hint(redis_client, key) == (None, 8)
    job_tasks._update_dispatch_batch_hint(redis_client, key)
    observed, limit = job_tasks._dispatch_batch_hint(redis_client, key)
    assert limit == 1
    assert 0 < redis_client.ttl(key) <= 300
    job_tasks._update_dispatch_batch_hint(redis_client, key, observed=observed, limit=2)
    old_success, limit = job_tasks._dispatch_batch_hint(redis_client, key)
    assert limit == 2
    job_tasks._update_dispatch_batch_hint(redis_client, key)
    new_timeout = redis_client.get(key)
    job_tasks._update_dispatch_batch_hint(
        redis_client, key, observed=old_success, limit=3
    )
    assert redis_client.get(key) == new_timeout
    assert job_tasks._dispatch_batch_hint(redis_client, key)[1] == 1
    redis_client.pexpire(key, 0)
    assert job_tasks._dispatch_batch_hint(redis_client, key) == (None, 8)


def test_successful_batches_recover_gradually_without_reducing_the_window(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    jobs, now = seed_jobs(factory, seed, 10)
    key = f"sync:dispatch-batch:v1:{job_tasks.settings.TIKTOK_APP_ID}:report"
    job_tasks._update_dispatch_batch_hint(redis_client, key)
    original = job_tasks._dispatch_report_jobs
    limits = []

    def report(*args, **kwargs):
        limits.append(kwargs["max_claims"])
        return original(*args, **kwargs)

    monkeypatch.setattr(job_tasks, "_dispatch_report_jobs", report)
    assert job_tasks._dispatch_pending(engine, now, budget_seconds=20) == 10
    assert limits[:4] == [1, 2, 3, 4]
    assert job_tasks._dispatch_batch_hint(redis_client, key)[1] == 5
    with factory() as db:
        assert all(db.get(ReportSyncJob, job).status == "RUNNING" for job in jobs)


@pytest.mark.parametrize("failed_batch", [False, True])
def test_hint_write_failure_preserves_commits_and_rollbacks(
    committed_sync_seed, redis_client, monkeypatch, failed_batch
):
    from app.modules.reporting.query_budget import QueryBudgetExceeded

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    jobs, now = seed_jobs(factory, seed, 2)
    key = f"sync:dispatch-batch:v1:{job_tasks.settings.TIKTOK_APP_ID}:report"
    if not failed_batch:
        job_tasks._update_dispatch_batch_hint(redis_client, key)
    original_cost = dispatcher.reserve_execution_cost
    calls = 0

    def cost(session, **kwargs):
        nonlocal calls
        calls += 1
        if failed_batch and calls == 2:
            raise QueryBudgetExceeded()
        return original_cost(session, **kwargs)

    original_set, original_eval = Redis.set, Redis.eval

    def unavailable_set(client, name, *args, **kwargs):
        if name == key:
            raise ConnectionError("batch hint unavailable")
        return original_set(client, name, *args, **kwargs)

    def unavailable_eval(client, script, count, *args):
        if args[0] == key:
            raise ConnectionError("batch hint unavailable")
        return original_eval(client, script, count, *args)

    monkeypatch.setattr(dispatcher, "reserve_execution_cost", cost)
    monkeypatch.setattr(Redis, "set", unavailable_set)
    monkeypatch.setattr(Redis, "eval", unavailable_eval)
    expected = 0 if failed_batch else 2
    assert job_tasks._dispatch_pending(engine, now, budget_seconds=4) == expected
    with factory() as db:
        assert (
            sum(db.get(ReportSyncJob, job).status == "RUNNING" for job in jobs)
            == expected
        )
        for model in (PendingDispatch, SyncCostLedger):
            assert (
                len(
                    db.exec(
                        select(model).where(model.tenant_id == seed.tenant_id)
                    ).all()
                )
                == expected
            )
    endpoint = report_endpoint("ad_day")
    for n in range(2):
        keys = occupancy_keys(
            app_scope=job_tasks.settings.TIKTOK_APP_ID,
            endpoint=endpoint,
            rate_group=endpoint,
            tenant_id=seed.tenant_id,
            advertiser_id=f"batch-{n}",
        )
        assert (
            all(redis_client.zcard(key) == 0 for key in keys)
            if failed_batch
            else all(redis_client.zcard(key) > 0 for key in keys)
        )


def test_repeated_short_budgets_commit_progress_without_global_penalty(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    jobs, now = seed_jobs(factory, seed, 6)
    original = dispatcher.reserve_execution_cost

    def slow_claim(session, **kwargs):
        # 每条能在预算内提交，四条不能；真实statement_timeout触发整批回滚。
        session.execute(text("SELECT pg_sleep(0.3)"))
        return original(session, **kwargs)

    monkeypatch.setattr(dispatcher, "reserve_execution_cost", slow_claim)

    def assert_consistent(expected):
        with factory() as db:
            running = [
                db.get(ReportSyncJob, identity)
                for identity in jobs
                if db.get(ReportSyncJob, identity).status == "RUNNING"
            ]
            assert len(running) == expected
            tokens = {str(job.claim_token) for job in running}
            heads = db.exec(
                select(SyncSchedule).where(SyncSchedule.tenant_id == seed.tenant_id)
            ).all()
            assert {
                str(head.lease_token) for head in heads if head.lease_token
            } == tokens
            for model in (PendingDispatch, SyncCostLedger):
                rows = db.exec(
                    select(model).where(model.tenant_id == seed.tenant_id)
                ).all()
                assert len(rows) == expected
            if not expected:
                assert all(head.generation == 0 for head in heads)
                assert all(head.last_dispatched_at is None for head in heads)
        endpoint = report_endpoint("ad_day")
        members = set()
        for n in range(6):
            keys = occupancy_keys(
                app_scope=job_tasks.settings.TIKTOK_APP_ID,
                endpoint=endpoint,
                rate_group=endpoint,
                tenant_id=seed.tenant_id,
                advertiser_id=f"batch-{n}",
            )
            for key in keys:
                members.update(redis_client.zrange(key, 0, -1))
        assert members == tokens

    # 短片从首轮就单条提交；无需先整批回滚、再永久压低后续完整唤醒。
    committed = 0
    key = f"sync:dispatch-batch:v1:{job_tasks.settings.TIKTOK_APP_ID}:report"
    for _ in range(6):
        claimed = job_tasks._dispatch_pending(engine, now, budget_seconds=0.8)
        assert claimed > 0, "重复唤醒必须能提交进展，不能再次撤销同一前缀"
        committed += claimed
        assert_consistent(committed)
        assert redis_client.get(key) is None
        if committed == len(jobs):
            break
    assert committed == len(jobs)


@pytest.mark.parametrize("budget,should_reduce", [(0.4, False), (4, True)])
@pytest.mark.usefixtures("committed_sync_seed")
def test_only_full_dispatch_budget_timeout_reduces_global_batch(
    redis_client,
    monkeypatch,
    budget,
    should_reduce,
):
    from app.modules.reporting.query_budget import QueryBudgetExceeded

    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    key = f"sync:dispatch-batch:v1:{job_tasks.settings.TIKTOK_APP_ID}:report"
    job_tasks._update_dispatch_batch_hint(redis_client, key, limit=4)
    before = redis_client.get(key)
    redis_client.pexpire(key, 60_000)

    def blocked(session, *_args, **_kwargs):
        session.execute(text("SELECT pg_sleep(6)"))
        raise QueryBudgetExceeded()

    monkeypatch.setattr(job_tasks, "_dispatch_report_jobs", blocked)
    assert (
        job_tasks._dispatch_pending(engine, datetime.now(UTC), budget_seconds=budget)
        == 0
    )
    if should_reduce:
        assert job_tasks._dispatch_batch_hint(redis_client, key)[1] == 1
        assert redis_client.get(key) != before and redis_client.pttl(key) > 60_000
    else:
        assert redis_client.get(key) == before and 0 < redis_client.pttl(key) <= 60_000


def test_short_dispatch_slice_commits_one_without_global_batch_penalty(
    committed_sync_seed,
    redis_client,
    monkeypatch,
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    jobs, now = seed_jobs(factory, seed, 2)
    key = f"sync:dispatch-batch:v1:{job_tasks.settings.TIKTOK_APP_ID}:report"
    job_tasks._update_dispatch_batch_hint(redis_client, key, limit=4)
    before = redis_client.get(key)
    original = dispatcher.reserve_execution_cost

    def slow_claim(session, **kwargs):
        session.execute(text("SELECT pg_sleep(0.25)"))
        return original(session, **kwargs)

    monkeypatch.setattr(dispatcher, "reserve_execution_cost", slow_claim)
    assert job_tasks._dispatch_pending(engine, now, budget_seconds=0.4) == 1
    assert redis_client.get(key) == before
    with factory() as db:
        assert (
            sum(
                db.get(ReportSyncJob, identity).status == "RUNNING" for identity in jobs
            )
            == 1
        )
        for model in (PendingDispatch, SyncCostLedger):
            assert (
                len(
                    db.exec(
                        select(model).where(model.tenant_id == seed.tenant_id)
                    ).all()
                )
                == 1
            )
