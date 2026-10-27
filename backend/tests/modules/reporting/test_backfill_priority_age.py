"""过期热报进入补数池后按补数顺序轮转，不继承实时特权。"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.modules.reporting import jobs
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_jobs import _route


def request(session, seed, now, *, tier, days_ago, due_at, created_at=None):
    day = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date() - timedelta(days=days_ago)
    job_id = jobs.enqueue_job(
        session,
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        actor_id=seed.actor_id,
        frozen_route=_route(seed),
        kind="material_day",
        tier=tier,
        advertiser_ids=["acc-1"],
        start=day,
        end=day,
        due_at=due_at,
    )
    job = session.get(ReportSyncJob, job_id)
    if created_at is not None:
        job.created_at = created_at
    session.flush()
    return job


@pytest.mark.parametrize("expired_tier", ["closeout", "hot", "material"])
@pytest.mark.parametrize("waiting_tier", ["daily", "history"])
def test_expired_live_facts_cannot_overtake_older_due_backfill(
    session, sync_seed, expired_tier, waiting_tier
):
    now = datetime.now(UTC)
    waiting = request(
        session,
        sync_seed,
        now,
        tier=waiting_tier,
        days_ago=10,
        due_at=now - timedelta(hours=2),
        created_at=now - timedelta(days=2),
    )
    request(
        session,
        sync_seed,
        now,
        tier=expired_tier,
        days_ago=2,
        due_at=now - timedelta(minutes=1),
    )

    claimed = jobs.claim_jobs(
        session, now=now, limit=1, lane="backfill", backfill_group="facts"
    )

    assert [job.id for job in claimed] == [waiting.id]


def test_continuations_and_new_expired_arrivals_rotate_behind_waiting_facts(
    session, sync_seed
):
    now = datetime.now(UTC)
    continuation = request(
        session,
        sync_seed,
        now,
        tier="closeout",
        days_ago=2,
        due_at=now - timedelta(hours=3),
    )
    waiting = request(
        session,
        sync_seed,
        now,
        tier="daily",
        days_ago=10,
        due_at=now - timedelta(hours=2),
    )
    assert (
        jobs.claim_jobs(session, now=now, limit=1, lane="backfill")[0].id
        == continuation.id
    )
    # 分页或本地准入让出后，原任务保留身份但到期时间前进；它不能反复抢回唯一槽。
    continuation.status = "WAITING"
    continuation.claim_token = continuation.claimed_until = None
    continuation.due_at = now + timedelta(seconds=1)
    request(
        session,
        sync_seed,
        now,
        tier="material",
        days_ago=3,
        due_at=now + timedelta(seconds=1),
    )
    session.flush()

    claimed = jobs.claim_jobs(
        session, now=now + timedelta(seconds=2), limit=1, lane="backfill"
    )

    assert [job.id for job in claimed] == [waiting.id]


def test_aged_fact_backoff_is_respected_then_precedes_later_arrivals(
    session, sync_seed
):
    now = datetime.now(UTC)
    retry_at = now + timedelta(minutes=1)
    waiting = request(
        session,
        sync_seed,
        now,
        tier="history",
        days_ago=10,
        due_at=retry_at,
        created_at=now - timedelta(days=2),
    )
    assert jobs.claim_jobs(session, now=now, limit=1, lane="backfill") == []
    request(
        session,
        sync_seed,
        now,
        tier="closeout",
        days_ago=2,
        due_at=retry_at + timedelta(seconds=1),
    )

    claimed = jobs.claim_jobs(
        session, now=retry_at + timedelta(seconds=2), limit=1, lane="backfill"
    )

    assert [job.id for job in claimed] == [waiting.id]


def test_current_and_yesterday_closeout_remain_in_their_own_lanes(session, sync_seed):
    now = datetime.now(UTC)
    live = request(session, sync_seed, now, tier="material", days_ago=0, due_at=now)
    closeout = request(session, sync_seed, now, tier="closeout", days_ago=1, due_at=now)

    assert jobs.claim_jobs(session, now=now, limit=3, lane="backfill") == []
    assert [
        job.id for job in jobs.claim_jobs(session, now=now, limit=3, lane="live")
    ] == [live.id]
    assert [
        job.id for job in jobs.claim_jobs(session, now=now, limit=3, lane="closeout")
    ] == [closeout.id]
