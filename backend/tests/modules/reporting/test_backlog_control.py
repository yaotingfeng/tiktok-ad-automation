"""持续热刷下历史必须进展；需求合并不能吞掉在途新增或跨授权。"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import select

from app.modules.reporting import job_tasks
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.test_jobs import _route


def request(session, seed, day, **overrides):
    args = {
        "tenant_id": seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": seed.actor_id,
        "frozen_route": _route(seed),
        "kind": "creative_day",
        "tier": "material",
        "advertiser_ids": ["acc-1"],
        "start": day,
        "end": day,
    }
    args.update(overrides)
    return enqueue_job(session, **args)


def test_history_has_capacity_during_continuous_current_arrivals(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 4)
    old = request(
        session, sync_seed, today - timedelta(days=5), due_at=now - timedelta(hours=2)
    )
    for i in range(12):
        request(
            session,
            sync_seed,
            today,
            campaign_ids=[str(i)],
            due_at=now - timedelta(minutes=1),
        )
    job_tasks._dispatch_report_jobs(session, now)
    assert session.get(ReportSyncJob, old).status == "RUNNING"


def test_history_dates_have_separate_buckets_and_frozen_routes(session, sync_seed):
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    first = request(session, sync_seed, today - timedelta(days=5))
    second = request(session, sync_seed, today - timedelta(days=3))
    assert second is not None and second != first
    job = session.get(ReportSyncJob, first)
    assert (job.start_date, job.end_date) == (
        today - timedelta(days=5),
        today - timedelta(days=5),
    )
    assert request(session, sync_seed, today) is not None
    route = _route(sync_seed) | {"binding_revision": 1}
    assert (
        request(session, sync_seed, today - timedelta(days=4), frozen_route=route)
        is not None
    )


def test_running_work_keeps_a_successor_for_new_same_day_demand(session, sync_seed):
    day = date(2026, 10, 5)
    first = request(session, sync_seed, day)
    job = session.get(ReportSyncJob, first)
    assert (
        claim_jobs(session, now=datetime.now(UTC) + timedelta(seconds=1), limit=1)[0].id
        == first
    )
    from app.modules.sync_control.models import SyncExecutionScope

    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == first)
    ).one()
    frozen = (child.frozen_revision, child.frozen_requested_after)
    successor = request(session, sync_seed, day)
    assert successor is not None and successor != first
    session.refresh(child)
    assert (child.frozen_revision, child.frozen_requested_after) == frozen
    assert request(session, sync_seed, day) is None
    job.status, job.finished_at = "COMPLETE", datetime.now(UTC)
    session.flush()
    active = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.status == "QUEUED")
    ).all()
    assert [j.id for j in active] == [successor]


def test_concurrent_requests_merge_and_do_not_wait_for_running_reader(
    committed_sync_seed,
):
    from concurrent.futures import ThreadPoolExecutor

    seed, factory = committed_sync_seed
    day = date(2026, 10, 5)
    with factory() as session:
        original = request(session, seed, day)
        job = session.get(ReportSyncJob, original)
        job.status, job.claim_token, job.claimed_until = (
            "RUNNING",
            uuid4(),
            datetime.now(UTC) + timedelta(minutes=4),
        )
        session.commit()
    with factory() as running:
        running.get(ReportSyncJob, original, with_for_update=True)

        def enqueue(offset):
            with factory() as session:
                result = request(session, seed, day - timedelta(days=offset))
                session.commit()
                return result

        with ThreadPoolExecutor(2) as workers:
            futures = [workers.submit(enqueue, i) for i in (1, 2)]
            results = [f.result(timeout=5) for f in futures]
        assert sum(r is not None for r in results) == 1
    with factory() as session:
        queued = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.status == "QUEUED",
            )
        ).all()
        assert {(job.start_date, job.end_date) for job in queued} == {
            (date(2026, 10, 3), date(2026, 10, 4)),
        }
        assert session.get(ReportSyncJob, original).start_date == day


def test_legacy_adoption_preserves_each_frozen_scope_and_checkpoint(session, sync_seed):
    from app.modules.sync_control.report_demand import adopt_report_job

    base = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "kind": "creative_day",
        "priority": 3,
        "advertiser_ids": ["acc-1"],
        "due_at": datetime.now(UTC),
    }
    old = ReportSyncJob(
        **base,
        job_key=uuid4().hex,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 1),
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        progress={"overview_ad_ids": ["a1"], "next_page": 3},
    )
    second = ReportSyncJob(
        **base,
        job_key=uuid4().hex,
        start_date=date(2026, 9, 3),
        end_date=date(2026, 9, 3),
    )
    session.add_all([old, second])
    session.flush()
    assert adopt_report_job(session, old) > 0
    assert adopt_report_job(session, second) > 0
    assert old.end_date == old.start_date == date(2026, 9, 1)
    assert old.progress["next_page"] == 3
    assert old.progress["overview_ad_ids"] == ["a1"]
    assert second.status == "QUEUED"
    assert second.start_date == second.end_date == date(2026, 9, 3)
    assert "merged_into" not in second.progress
    assert adopt_report_job(session, old) == 0


def test_history_keeps_progress_when_every_round_adds_more_current_work(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 4)
    old = request(session, sync_seed, today - timedelta(days=3), due_at=now)
    seen = set()
    for turn in range(12):
        for index in range(6):
            request(
                session,
                sync_seed,
                today,
                campaign_ids=[f"live-{turn}-{index}"],
                due_at=now,
            )
        running = claim_jobs(session, now=now, limit=4)
        for row in running:
            seen.add("history" if row.id == old else "current")
            row.status = "WAITING" if row.id == old else "COMPLETE"
            row.due_at = now + timedelta(seconds=1)
            from app.modules.sync_control.schedule import record_report_outcome

            record_report_outcome(session, row)
            row.claim_token = row.claimed_until = None
        session.flush()
        now += timedelta(seconds=1)
    assert seen == {"history", "current"}


@pytest.mark.parametrize("facts", [0, 6])
def test_historical_links_use_backfill_pool_and_share_with_facts(
    session, sync_seed, monkeypatch, facts
):
    from app.jobs.models import PendingDispatch

    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 3)
    for index in range(6):
        request(session, sync_seed, today, campaign_ids=[str(index)], due_at=now)
        request(
            session,
            sync_seed,
            today - timedelta(days=3),
            campaign_ids=[str(index)],
            due_at=now,
        )
    for index in range(facts):
        request(
            session,
            sync_seed,
            today - timedelta(days=10 + index),
            kind="ad_day",
            tier="history",
            due_at=now,
        )
    seen = set()
    for turn in range(16):
        running = claim_jobs(session, now=now + timedelta(seconds=turn), limit=5)
        for row in running:
            seen.add((row.kind, row.start_date < today))
            dispatch = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_key.startswith(f"report-job:{row.id}:")
                )
            ).one()
            assert dispatch.task_name == (
                "reporting.backfill_step"
                if row.kind == "ad_day" or row.start_date < today
                else "reporting.job_step"
            )
            row.status = "COMPLETE"
            from app.modules.sync_control.schedule import record_report_outcome

            record_report_outcome(session, row)
            row.claim_token = row.claimed_until = None
        session.flush()
    assert {("creative_day", False), ("creative_day", True)} <= seen
    if facts:
        assert ("ad_day", True) in seen


@pytest.mark.parametrize("origin_tier", ["daily", "history", "manual"])
def test_backfill_link_continuations_cannot_starve_other_origin_priorities(
    session, sync_seed, monkeypatch, origin_tier
):
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 2)
    old = request(
        session,
        sync_seed,
        today - timedelta(days=4),
        tier=origin_tier,
        due_at=now - timedelta(minutes=10),
    )
    for index in range(3):
        request(
            session,
            sync_seed,
            today - timedelta(days=4),
            tier="material",
            campaign_ids=[str(index)],
            due_at=now,
        )
    job_tasks._dispatch_report_jobs(session, now)
    assert session.get(ReportSyncJob, old).status == "RUNNING"
