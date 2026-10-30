"""后台四进程中，目录与补链不能共同占完历史事实的执行机会。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_directory_direct_delivery import directory
from tests.modules.reporting.test_jobs import _route


def pending(session, seed, kind, offset, now, *, future=False):
    day = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date() - timedelta(days=offset + 2)
    return enqueue_job(
        session,
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        actor_id=seed.actor_id,
        frozen_route=_route(seed),
        kind=kind,
        tier="history",
        advertiser_ids=["acc-1"],
        campaign_ids=[str(offset)],
        start=day,
        end=day,
        due_at=now + timedelta(minutes=5) if future else now - timedelta(minutes=1),
    )


def fill(session, seed, now, *, facts=True, future=False):
    for i in range(10):
        if facts:
            pending(session, seed, "material_day", i, now, future=future)
        pending(session, seed, "material_links", i, now)
        run = directory(session, seed)
        run.workload = "background"
    session.flush()


@pytest.mark.parametrize("directory_first", [True, False])
def test_fact_backlog_leaves_at_most_two_other_steps_in_shared_pool(
    session, sync_seed, directory_first
):
    now = datetime.now(UTC)
    fill(session, sync_seed, now)
    dispatchers = [job_tasks._dispatch_report_jobs, job_tasks._dispatch_directories]
    if directory_first:
        dispatchers.reverse()
    for dispatch in dispatchers:
        dispatch(session, now)
    links = session.exec(
        select(ReportSyncJob).where(
            ReportSyncJob.kind == "material_links", ReportSyncJob.status == "RUNNING"
        )
    ).all()
    facts = session.exec(
        select(ReportSyncJob).where(
            ReportSyncJob.kind == "material_day", ReportSyncJob.status == "RUNNING"
        )
    ).all()
    directories = session.exec(
        select(PendingDispatch).where(PendingDispatch.task_name == "ads.sync_step")
    ).all()
    assert len(links) == 1
    assert len(directories) == 1
    assert len(facts) >= 2
    # 连续补位不能越过共同服务四进程的保护条件。
    for dispatch in dispatchers:
        assert dispatch(session, now) == 0


@pytest.mark.parametrize("future", [False, True])
def test_idle_fact_share_can_be_borrowed(session, sync_seed, future):
    now = datetime.now(UTC)
    fill(session, sync_seed, now, facts=future, future=future)
    job_tasks._dispatch_report_jobs(session, now)
    job_tasks._dispatch_directories(session, now)
    links = session.exec(
        select(ReportSyncJob).where(
            ReportSyncJob.kind == "material_links", ReportSyncJob.status == "RUNNING"
        )
    ).all()
    messages = session.exec(
        select(PendingDispatch).where(PendingDispatch.task_name == "ads.sync_step")
    ).all()
    assert len(links) > 1 and len(messages) > 1


def test_realtime_directory_keeps_its_own_window(session, sync_seed):
    now = datetime.now(UTC)
    fill(session, sync_seed, now)
    for _ in range(40):
        directory(session, sync_seed)
    session.flush()
    job_tasks._dispatch_directories(session, now)
    messages = session.exec(
        select(PendingDispatch).where(PendingDispatch.task_name == "ads.targeted_step")
    ).all()
    assert len(messages) == 32
    assert all(
        session.get(AdDirectoryRun, UUID(m.payload["run_id"])).workload == "live"
        for m in messages
    )


def test_concurrent_dispatchers_preserve_background_fact_share(committed_sync_seed):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        fill(db, seed, now)
    barrier = Barrier(4)

    def dispatch(index):
        barrier.wait(timeout=10)
        with factory() as db, db.begin():
            return (
                job_tasks._dispatch_report_jobs
                if index % 2
                else job_tasks._dispatch_directories
            )(db, now)

    with ThreadPoolExecutor(4) as executor:
        list(executor.map(dispatch, range(4)))
    with factory() as db:
        running = db.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.status == "RUNNING",
            )
        ).all()
        assert sum(r.kind == "material_links" for r in running) == 1
        assert sum(r.kind != "material_links" for r in running) == 5
        messages = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "ads.sync_step",
            )
        ).all()
        assert len(messages) == 1


def test_new_due_facts_reclaim_borrowed_link_window(session, sync_seed):
    now = datetime.now(UTC)
    fill(session, sync_seed, now, facts=False)
    job_tasks._dispatch_report_jobs(session, now)
    links = session.exec(
        select(ReportSyncJob).where(
            ReportSyncJob.kind == "material_links", ReportSyncJob.status == "RUNNING"
        )
    ).all()
    assert len(links) == 6
    pending(session, sync_seed, "material_day", 50, now)
    # 不取消已领取的工作；一个旧步骤释放后，候补必须交给已到期事实。
    links[0].status = "WAITING"
    links[0].claim_token = links[0].claimed_until = None
    links[0].due_at = now + timedelta(minutes=1)
    session.flush()
    assert job_tasks._dispatch_report_jobs(session, now) == 1
    facts = session.exec(
        select(ReportSyncJob).where(
            ReportSyncJob.kind == "material_day", ReportSyncJob.status == "RUNNING"
        )
    ).all()
    assert len(facts) == 1


def test_paused_fact_budget_does_not_reserve_directory_capacity(session, sync_seed):
    now = datetime.now(UTC)
    fill(session, sync_seed, now)
    assert job_tasks._dispatch_directories(session, now, allow_backfill=False) == 8


def test_historical_directory_slot_keeps_fifo_live_dependencies_use_separate_pool(
    session, sync_seed
):
    now = datetime.now(UTC)
    pending(session, sync_seed, "material_day", 0, now)
    old_full = directory(session, sync_seed)
    old_full.query = old_full.query | {"ids": []}
    old_full.workload = "background"
    old_full.created_at = now - timedelta(hours=2)
    dependency = directory(session, sync_seed)
    dependency.workload = "background"
    dependency.created_at = now - timedelta(hours=1)
    live = directory(session, sync_seed)
    session.flush()
    job_tasks._dispatch_directories(session, now)
    messages = session.exec(select(PendingDispatch)).all()
    assert {m.payload["run_id"] for m in messages} == {str(old_full.id), str(live.id)}


@pytest.mark.parametrize(
    "state", ["lease", "unpublished", "recent", "lost", "old_generation", "live"]
)
def test_continuation_capacity_uses_current_message_and_lease(
    session, sync_seed, state
):
    from app.modules.ads.directory_dispatch import (
        background_continuation_available,
        enqueue_directory_step,
    )

    now = datetime.now(UTC)
    own, other = directory(session, sync_seed), directory(session, sync_seed)
    own.workload = other.workload = "background"
    if state == "lease":
        other.claimed_until = now + timedelta(minutes=1)
    else:
        if state == "live":
            other.workload = "live"
        enqueue_directory_step(session, other)
        message = session.exec(select(PendingDispatch)).one()
        if state != "unpublished":
            message.published_at = now - timedelta(minutes=11 if state == "lost" else 1)
        if state == "old_generation":
            other.claim_generation += 1
    session.flush()
    assert background_continuation_available(session, own, now) == (
        state in {"lost", "old_generation", "live"}
    )
