"""目录补位唤醒按当前五秒桶合并，业务页和执行消息仍独立持久。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.ads import directory_dispatch
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from tests.modules.ads.test_directory_requests import _route


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    class Clock(datetime):
        at = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return cls.at

    monkeypatch.setattr(directory_dispatch, "datetime", Clock)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    return Clock


def _signal_run(seed, **changes):
    return SimpleNamespace(
        **(
            {
                "tenant_id": seed.tenant_id,
                "actor_id": seed.actor_id,
                "id": uuid4(),
                "claim_generation": 1,
                "completed_at": datetime(2025, 1, 1, tzinfo=UTC),
            }
            | changes
        )
    )


def _wakeups(session, tenant):
    return session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == tenant,
            PendingDispatch.task_name == "reporting.plan_due",
        )
    ).all()


def test_many_runs_and_generations_share_one_current_time_bucket(
    session, sync_seed, clock
):
    for generation in range(1, 8):
        directory_dispatch.enqueue_directory_refill(
            session, _signal_run(sync_seed, claim_generation=generation)
        )
    rows = _wakeups(session, sync_seed.tenant_id)
    assert len(rows) == 1
    assert rows[0].payload == {"dispatch_only": True}
    assert rows[0].actor_id == sync_seed.actor_id
    assert rows[0].task_key == (
        f"directory-refill:{sync_seed.tenant_id}:{sync_seed.actor_id}:"
        f"{int(clock.at.timestamp()) // 5}"
    )


def test_actor_and_tenant_scopes_do_not_merge(session, sync_seed):
    other_actor, other_tenant = uuid4(), uuid4()
    for changes in ({}, {"actor_id": other_actor}, {"tenant_id": other_tenant}):
        for _ in range(2):
            directory_dispatch.enqueue_directory_refill(
                session, _signal_run(sync_seed, **changes)
            )
    own = _wakeups(session, sync_seed.tenant_id)
    assert len(own) == 2 and {row.actor_id for row in own} == {
        sync_seed.actor_id,
        other_actor,
    }
    assert len(_wakeups(session, other_tenant)) == 1


def test_new_bucket_uses_enqueue_clock_even_with_old_transaction_and_published_signal(
    session, sync_seed, clock
):
    # PostgreSQL事务时钟和run时间均不前进；只有真实入队时刻决定桶。
    transaction_at = session.execute(
        text("SELECT transaction_timestamp()")
    ).scalar_one()
    clock.at = clock.at.replace(second=4, microsecond=999999)
    run = _signal_run(sync_seed)
    directory_dispatch.enqueue_directory_refill(session, run)
    first = _wakeups(session, sync_seed.tenant_id)[0]
    first.published_at = clock.at
    session.flush()
    directory_dispatch.enqueue_directory_refill(session, _signal_run(sync_seed))
    assert len(_wakeups(session, sync_seed.tenant_id)) == 1
    assert first.published_at is not None  # 同桶已消费唤醒不复活，30秒Beat仍兜底。
    clock.at += timedelta(microseconds=1)
    directory_dispatch.enqueue_directory_refill(session, run)
    assert (
        session.execute(text("SELECT transaction_timestamp()")).scalar_one()
        == transaction_at
    )
    rows = _wakeups(session, sync_seed.tenant_id)
    assert len(rows) == 2 and len({row.task_key for row in rows}) == 2
    assert sum(row.published_at is None for row in rows) == 1


def test_disabled_sync_does_not_enqueue(session, sync_seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    directory_dispatch.enqueue_directory_refill(session, _signal_run(sync_seed))
    assert _wakeups(session, sync_seed.tenant_id) == []


def _business(session, seed):
    run = AdDirectoryRun(
        tenant_id=seed.tenant_id,
        actor_id=seed.actor_id,
        bc_id="bc-a",
        advertiser_id="acc-1",
        connection_id=seed.connection.id,
        frozen_route=_route(seed),
        channel="OFFICIAL_API",
        kind="ad",
        ad_type="SMART_PLUS",
        workload="background",
        partition_key="a" * 64,
        query={"ids": [], "parent_ids": [], "include_deleted": True},
        status="RUNNING",
        claim_generation=1,
        next_page=2,
    )
    session.add(run)
    session.flush()
    session.add(
        AdDirectoryPage(
            run_id=run.id,
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            page=1,
            claim_generation=1,
            complete=True,
            items=[],
            materials=[],
        )
    )
    directory_dispatch.enqueue_directory_step(session, run, publish_only=True)
    directory_dispatch.enqueue_directory_refill(session, run)
    return run.id


def test_real_concurrent_transactions_merge_signals_without_merging_business(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    barrier = Barrier(6)

    def complete():
        with factory() as session, session.begin():
            barrier.wait(timeout=10)
            return _business(session, seed)

    with ThreadPoolExecutor(max_workers=6) as pool:
        runs = list(pool.map(lambda _: complete(), range(6)))
    with factory() as session:
        assert len(_wakeups(session, seed.tenant_id)) == 1
        assert len(set(runs)) == 6
        for run_id in runs:
            assert session.get(AdDirectoryRun, run_id).next_page == 2
            assert session.get(AdDirectoryPage, (run_id, 1)).complete
        steps = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "ads.publish_step",
            )
        ).all()
        assert {step.payload["run_id"] for step in steps} == {
            str(run_id) for run_id in runs
        }
        assert len(steps) == 6


def test_rollback_leaves_bucket_available_and_preserves_committed_sibling(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    with factory() as session:
        rolled_back = _business(session, seed)
        session.rollback()
    with factory() as session:
        assert not _wakeups(session, seed.tenant_id)
        assert session.get(AdDirectoryRun, rolled_back) is None
    with factory() as session, session.begin():
        committed = _business(session, seed)
    with factory() as session:
        sibling = _business(session, seed)
        session.rollback()
    with factory() as session:
        assert len(_wakeups(session, seed.tenant_id)) == 1
        assert session.get(AdDirectoryRun, committed) is not None
        assert session.get(AdDirectoryPage, (committed, 1)).complete
        assert session.get(AdDirectoryRun, sibling) is None
