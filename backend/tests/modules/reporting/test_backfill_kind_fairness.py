"""历史事实不能因素材任务数量大而挤掉广告、账户及小时事实。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlmodel import select

from app.modules.reporting import job_tasks, jobs
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_jobs import _route


def pending(session, seed, now, kind, *, count=1, due_age=1, priority=3):
    rows = [
        ReportSyncJob(
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            actor_id=seed.actor_id,
            frozen_route=_route(seed),
            kind=kind,
            priority=priority,
            advertiser_ids=["acc-1"],
            start_date=now.date() - timedelta(days=10),
            end_date=now.date() - timedelta(days=10),
            job_key=uuid4().hex,
            due_at=now - timedelta(hours=due_age) + timedelta(microseconds=index),
            created_at=now - timedelta(hours=12),
        )
        for index in range(count)
    ]
    session.add_all(rows)
    session.flush()
    return rows


@pytest.mark.parametrize("group", ["facts", None])
def test_thousands_of_material_jobs_cannot_hide_other_fact_kinds(
    session, sync_seed, group
):
    now = datetime.now(UTC)
    material = pending(session, sync_seed, now, "material_day", count=2500, due_age=4)
    ad = pending(session, sync_seed, now, "ad_day", due_age=3)[0]
    account = pending(session, sync_seed, now, "account_day", due_age=2)[0]

    claimed = jobs.claim_jobs(
        session, now=now, limit=3, lane="backfill", backfill_group=group
    )

    assert {row.id for row in claimed} == {material[0].id, ad.id, account.id}
    assert all(row.frozen_route == _route(sync_seed) for row in claimed)
    assert len({row.claim_token for row in claimed}) == 3


def test_existing_inflight_material_does_not_take_account_or_hour_slot(
    session, sync_seed
):
    now = datetime.now(UTC)
    material = pending(session, sync_seed, now, "material_day", count=8, due_age=4)
    material[0].status = "RUNNING"
    material[0].claim_token = uuid4()
    material[0].claimed_until = now + timedelta(minutes=2)
    pending(session, sync_seed, now, "campaign_hour", due_age=2)
    pending(session, sync_seed, now, "account_day", due_age=1)
    session.flush()

    claimed = jobs.claim_jobs(
        session, now=now, limit=2, lane="backfill", backfill_group="facts"
    )

    assert {row.kind for row in claimed} == {"campaign_hour", "account_day"}


def test_kind_internal_effective_priority_due_order_and_backoff_are_preserved(
    session, sync_seed
):
    now = datetime.now(UTC)
    history = pending(session, sync_seed, now, "ad_day", due_age=4, priority=4)[0]
    history.created_at = now - timedelta(days=2)
    daily = pending(session, sync_seed, now, "ad_day", due_age=3)[0]
    delayed = pending(session, sync_seed, now, "ad_day", due_age=-1)[0]
    pending(session, sync_seed, now, "ad_day", due_age=5, priority=4)
    session.flush()
    first = jobs.claim_jobs(
        session, now=now, limit=1, lane="backfill", backfill_group="facts"
    )[0]
    assert first.id == history.id
    first.status = "WAITING"
    first.claim_token = first.claimed_until = None
    first.due_at = now
    session.flush()

    second = jobs.claim_jobs(
        session, now=now, limit=1, lane="backfill", backfill_group="facts"
    )[0]

    assert second.id == daily.id
    assert delayed.status == "QUEUED"


@pytest.mark.parametrize("with_links", [False, True])
def test_total_window_and_links_reservation_with_capacity_borrowing(
    session, sync_seed, with_links
):
    now = datetime.now(UTC)
    pending(session, sync_seed, now, "material_day", count=20, due_age=4)
    pending(session, sync_seed, now, "ad_day", count=20, due_age=3)
    pending(session, sync_seed, now, "account_day", count=20, due_age=2)
    if with_links:
        pending(session, sync_seed, now, "material_links", count=20)

    assert job_tasks._dispatch_report_jobs(session, now) == 6
    running = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.status == "RUNNING")
    ).all()

    assert len(running) == 6
    assert sum(row.kind == "material_links" for row in running) == (
        3 if with_links else 0
    )
    for kind in ("material_day", "ad_day", "account_day"):
        assert sum(row.kind == kind for row in running) == (1 if with_links else 2)


def test_locked_head_is_skipped_and_next_same_kind_can_progress(committed_sync_seed):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        first, second = pending(session, seed, now, "ad_day", count=2)
    with factory() as holder, holder.begin():
        holder.exec(
            select(ReportSyncJob).where(ReportSyncJob.id == first.id).with_for_update()
        ).one()
        with factory() as claimant, claimant.begin():
            claimed = jobs.claim_jobs(
                claimant, now=now, limit=1, lane="backfill", backfill_group="facts"
            )
            assert [row.id for row in claimed] == [second.id]


def test_parallel_claimers_get_distinct_jobs_without_waiting(committed_sync_seed):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        for kind in ("material_day", "ad_day", "account_day"):
            pending(session, seed, now, kind, count=4)
    with factory() as first, first.begin():
        first_batch = jobs.claim_jobs(
            first, now=now, limit=3, lane="backfill", backfill_group="facts"
        )
        with factory() as second, second.begin():
            second_batch = jobs.claim_jobs(
                second, now=now, limit=3, lane="backfill", backfill_group="facts"
            )
            assert len(second_batch) == 3
            assert {row.id for row in first_batch}.isdisjoint(
                row.id for row in second_batch
            )
            assert {row.claim_token for row in first_batch}.isdisjoint(
                row.claim_token for row in second_batch
            )


def test_expired_claim_is_recovered_with_new_generation(session, sync_seed):
    now = datetime.now(UTC)
    job = pending(session, sync_seed, now, "account_day", due_age=-1)[0]
    old_token = uuid4()
    job.status = "RUNNING"
    job.claim_token = old_token
    job.claimed_until = now - timedelta(seconds=1)
    session.flush()

    claimed = jobs.claim_jobs(
        session, now=now, limit=1, lane="backfill", backfill_group="facts"
    )

    assert [row.id for row in claimed] == [job.id]
    assert job.claim_token != old_token
    assert job.claimed_until == now + jobs.LEASE


def test_lock_selection_uses_bounded_primary_key_probes(session, sync_seed):
    now = datetime.now(UTC)
    pending(session, sync_seed, now, "material_day", count=2500)
    connection = session.connection()
    queries = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "candidates AS (" in statement:
            queries.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert (
            len(
                jobs.claim_jobs(
                    session, now=now, limit=3, lane="backfill", backfill_group="facts"
                )
            )
            == 3
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    statement, parameters = queries[0]
    plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters
    ).scalar_one()[0]["Plan"]

    def walk(node):
        yield node
        for child in node.get("Plans", []):
            yield from walk(child)

    # 排名需要扫描既有候选一次；最终拿锁不能再把全部积压扫描一遍。
    lock_nodes = [node for node in walk(plan) if node["Node Type"] == "LockRows"]
    assert len(lock_nodes) == 1
    lock = lock_nodes[0]
    assert lock["Actual Loops"] == 3
    assert lock["Actual Rows"] == 1
    assert lock["Plans"][0]["Index Name"] == "report_sync_job_pkey"
