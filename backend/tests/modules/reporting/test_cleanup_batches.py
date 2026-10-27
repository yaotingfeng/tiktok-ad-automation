"""清理使用真实 PostgreSQL 验证有界页删除、提交及分类隔离。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlmodel import select

from app.modules.reporting import job_tasks
from app.modules.reporting.store_models import ReportSyncJob, ReportSyncPage
from tests.modules.reporting.test_jobs import D, _route


def _job(session, seed, *, identifier, expired=True, pages=0):
    now = datetime.now(UTC)
    job = ReportSyncJob(
        id=UUID(int=identifier),
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        priority=1,
        status="COMPLETE" if expired else "RUNNING",
        advertiser_ids=["acc-1"],
        start_date=D,
        end_date=D,
        job_key=f"cleanup-{identifier}",
        frozen_route=_route(seed),
        actor_id=seed.actor_id,
        due_at=now,
        finished_at=now - timedelta(days=8) if expired else None,
    )
    session.add(job)
    session.flush()
    for page in range(1, pages + 1):
        session.add(
            ReportSyncPage(
                job_id=job.id,
                page=page,
                tenant_id=seed.tenant_id,
                scope_key="x",
                rows=[],
                row_keys=[],
                total_page=pages,
                total_number=0,
            )
        )
    session.flush()
    return job.id


def test_job_pages_are_bounded_before_parent_deletion(session, sync_seed, monkeypatch):
    monkeypatch.setattr(job_tasks, "CLEANUP_BATCH_ROWS", 2, raising=False)
    old = _job(session, sync_seed, identifier=1, pages=5)
    active = _job(session, sync_seed, identifier=2, expired=False, pages=3)
    now = datetime.now(UTC)
    result = job_tasks.cleanup_store(session, now=now)
    assert session.get(ReportSyncJob, old) is not None
    assert result["material_pages"] == 2
    assert (
        len(
            session.exec(
                select(ReportSyncPage).where(ReportSyncPage.job_id == old)
            ).all()
        )
        == 3
    )
    job_tasks.cleanup_store(session, now=now)
    job_tasks.cleanup_store(session, now=now)
    job_tasks.cleanup_store(session, now=now)
    session.expire_all()
    assert session.get(ReportSyncJob, old) is None
    assert (
        len(
            session.exec(
                select(ReportSyncPage).where(ReportSyncPage.job_id == active)
            ).all()
        )
        == 3
    )


def test_bounded_scan_advances_past_live_jobs(session, sync_seed, monkeypatch):
    monkeypatch.setattr(job_tasks, "CLEANUP_SCAN_ROWS", 2, raising=False)
    for identifier in (10, 11, 12):
        _job(session, sync_seed, identifier=identifier, expired=False)
    old = _job(session, sync_seed, identifier=13)
    cursors = {}
    now = datetime.now(UTC)
    for _ in range(4):
        job_tasks.cleanup_store(session, now=now, categories=("jobs",), cursors=cursors)
    session.expire_all()
    assert session.get(ReportSyncJob, old) is None


def test_category_timeout_does_not_starve_other_cleanup(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting.query_models import QuerySnapshot

    now = datetime.now(UTC)
    snapshot = QuerySnapshot(
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        filter_digest="x" * 64,
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    session.add(snapshot)
    session.flush()
    snapshot_id = snapshot.id
    original = job_tasks._cleanup_category

    def blocked(session, *, category, **kwargs):
        if category == "jobs":
            session.connection().execute(text("SET LOCAL statement_timeout='20ms'"))
            session.connection().execute(text("SELECT pg_sleep(0.2)"))
        return original(session, category=category, **kwargs)

    monkeypatch.setattr(job_tasks, "_cleanup_category", blocked)
    cursors = {}
    result = job_tasks.cleanup_store(session, now=now, cursors=cursors)
    assert cursors["_limits"]["jobs"] == {"scan": 250, "batch": 250}
    assert cursors.get("jobs") is None
    assert result["snapshots"] == 1
    session.expire_all()
    assert session.get(QuerySnapshot, snapshot_id) is None


def test_task_commits_each_category_before_later_failure(
    committed_sync_seed, monkeypatch
):
    import os

    import pytest

    seed, factory = committed_sync_seed
    with factory() as session:
        old = _job(session, seed, identifier=101)
        session.commit()
    original = job_tasks._cleanup_category

    def fail_later(session, *, category, **kwargs):
        if category == "hours":
            # 本批失败之前，其他连接已能看见旧任务删除，不依赖最终统一提交。
            with factory() as observer:
                assert observer.get(ReportSyncJob, old) is None
            raise RuntimeError("later category failed")
        return original(session, category=category, **kwargs)

    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setattr(job_tasks, "_cleanup_category", fail_later)
    with pytest.raises(RuntimeError, match="later category failed"):
        job_tasks.cleanup_store_task.run()
    with factory() as observer:
        assert observer.get(ReportSyncJob, old) is None


def test_concurrent_task_does_not_advance_cleanup_cursor(redis_client, monkeypatch):
    import os

    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    cursor_key = job_tasks.CLEANUP_CURSOR_KEY
    lock = redis_client.lock("reporting:cleanup:lock:v1", timeout=120)
    redis_client.delete(cursor_key)
    assert lock.acquire(blocking=False)
    try:
        assert not any(job_tasks.cleanup_store_task.run().values())
        assert redis_client.hgetall(cursor_key) == {}
    finally:
        lock.release()
        redis_client.delete(cursor_key)


def test_locked_job_does_not_block_other_categories(committed_sync_seed):
    from app.modules.reporting.query_models import QuerySnapshot

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        old = _job(session, seed, identifier=201)
        _job(session, seed, identifier=202)
        snapshot = QuerySnapshot(
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            actor_id=seed.actor_id,
            filter_digest="x" * 64,
            created_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        snapshot_id = snapshot.id
        session.add(snapshot)
        session.commit()
    cursors = {}
    with factory() as blocker:
        blocker.get(ReportSyncJob, old, with_for_update=True)
        with factory() as cleaner:
            result = job_tasks.cleanup_store(
                cleaner, now=now, categories=("jobs", "snapshots"), cursors=cursors
            )
            cleaner.commit()
        assert result == {"jobs": 1, "snapshots": 1}
        assert cursors.get("jobs") is None
        with factory() as observer:
            assert observer.get(QuerySnapshot, snapshot_id) is None
            assert observer.get(ReportSyncJob, old) is not None
    with factory() as cleaner:
        assert (
            job_tasks.cleanup_store(
                cleaner, now=now, categories=("jobs",), cursors=cursors
            )["jobs"]
            == 1
        )
        cleaner.commit()


def test_terminal_cleanup_never_deletes_active_job_with_old_finished_at(
    session, sync_seed
):
    job_id = _job(session, sync_seed, identifier=301, expired=False, pages=3)
    job = session.get(ReportSyncJob, job_id)
    job.finished_at = datetime.now(UTC) - timedelta(days=20)
    session.add(job)
    session.flush()
    result = job_tasks.cleanup_store(session, now=datetime.now(UTC))
    assert result["jobs"] == result["material_pages"] == 0
    assert (
        len(
            session.exec(
                select(ReportSyncPage).where(ReportSyncPage.job_id == job_id)
            ).all()
        )
        == 3
    )


def test_task_drains_multiple_job_batches_and_persists_cursor(
    committed_sync_seed, monkeypatch, redis_client, record_property
):
    import os
    import time

    seed, factory = committed_sync_seed
    cursor_key = job_tasks.CLEANUP_CURSOR_KEY
    redis_client.delete(cursor_key)
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    with factory() as session:
        base_id = _job(session, seed, identifier=1000)
        template = session.get(ReportSyncJob, base_id).model_dump()
        session.connection().execute(
            ReportSyncJob.__table__.insert(),
            [
                dict(template, id=UUID(int=identifier), job_key=f"cleanup-{identifier}")
                for identifier in range(1001, 11000)
            ],
        )
        session.commit()
    started = time.monotonic()
    try:
        result = job_tasks.cleanup_store_task.run()
        elapsed = time.monotonic() - started
        assert result["jobs"] == 10000
        assert 0 < redis_client.ttl(cursor_key) <= 7 * 24 * 3600
        with factory() as observer:
            assert not observer.exec(
                select(ReportSyncJob.id).where(
                    ReportSyncJob.tenant_id == seed.tenant_id
                )
            ).all()
        record_property("cleanup_10000_jobs_seconds", round(elapsed, 3))
    finally:
        redis_client.delete(cursor_key)


def test_task_gives_every_category_a_turn_before_budget_exit(redis_client, monkeypatch):
    import os

    cursor_key = job_tasks.CLEANUP_CURSOR_KEY
    redis_client.delete(cursor_key)
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setattr(job_tasks, "DIRECTORY_PAGE_CLEANUP_SECONDS", 0)
    original = job_tasks._cleanup_category
    categories = []

    def observe(session, *, category, **kwargs):
        categories.append(category)
        return original(session, category=category, **kwargs)

    monkeypatch.setattr(job_tasks, "_cleanup_category", observe)
    try:
        job_tasks.cleanup_store_task.run()
        assert categories == list(job_tasks.CLEANUP_CATEGORIES)
    finally:
        redis_client.delete(cursor_key)


def _hour_rows(session, seed, *, now, include_boundary=False):
    from datetime import timezone
    from uuid import uuid4

    from app.modules.reporting.store_models import CampaignReportHour

    # 不同 UTC 偏移写入同一时间点；游标恢复后必须保持原时间意义和 30 天边界。
    offset = timezone(timedelta(hours=5, minutes=45))
    rows = [
        ("acc-1", f"recent-{index}", now - timedelta(days=1)) for index in range(3)
    ] + [("acc-2", f"expired-{index}", now - timedelta(days=31)) for index in range(2)]
    if include_boundary:
        rows.append(("acc-2", "boundary", now - timedelta(days=30)))
    for account, campaign, at in rows:
        session.add(
            CampaignReportHour(
                tenant_id=seed.tenant_id,
                advertiser_id=account,
                campaign_id=campaign,
                stat_hour=at.astimezone(offset),
                currency="USD",
                timezone="Asia/Kathmandu",
                fetched_at=now,
                sync_job_id=uuid4(),
            )
        )
    session.flush()


def test_hour_cleanup_resumes_composite_cursor_and_preserves_retention(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting.store_models import CampaignReportHour

    now = datetime.now(UTC)
    _hour_rows(session, sync_seed, now=now, include_boundary=True)
    monkeypatch.setattr(job_tasks, "CLEANUP_SCAN_ROWS", 2)
    monkeypatch.setattr(job_tasks, "CLEANUP_BATCH_ROWS", 1)
    cursors = {}
    removed = [
        job_tasks.cleanup_store(
            session, now=now, categories=("hours",), cursors=cursors
        )["hours"]
        for _ in range(8)
    ]
    assert sum(removed) == 2
    assert max(removed) == 1
    remaining = session.exec(
        select(CampaignReportHour).where(
            CampaignReportHour.tenant_id == sync_seed.tenant_id
        )
    ).all()
    assert {row.campaign_id for row in remaining} == {
        "recent-0",
        "recent-1",
        "recent-2",
        "boundary",
    }
    boundary = next(row for row in remaining if row.campaign_id == "boundary")
    assert boundary.stat_hour == now - timedelta(days=30)
    assert boundary.stat_hour.utcoffset() is not None


def test_hour_cleanup_restores_redis_cursor_across_task_invocations(
    committed_sync_seed, redis_client, monkeypatch
):
    import json
    import os

    from app.modules.reporting.store_models import CampaignReportHour

    seed, factory = committed_sync_seed
    with factory() as session:
        _hour_rows(session, seed, now=datetime.now(UTC))
        session.commit()
    cursor_key = job_tasks.CLEANUP_CURSOR_KEY
    redis_client.delete(cursor_key)
    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setattr(job_tasks, "CLEANUP_CATEGORIES", ("hours",))
    monkeypatch.setattr(job_tasks, "DIRECTORY_PAGE_CLEANUP_SECONDS", 0)
    monkeypatch.setattr(job_tasks, "CLEANUP_SCAN_ROWS", 2)
    monkeypatch.setattr(job_tasks, "CLEANUP_BATCH_ROWS", 1)
    try:
        assert job_tasks.cleanup_store_task.run()["hours"] == 0
        cursor = json.loads(redis_client.hget(cursor_key, "hours"))
        assert cursor[1] == "acc-1"
        assert datetime.fromisoformat(cursor[2]).utcoffset() is not None
        # 每次调用新建 Session，并重新读取真实 Redis，不借用上次内存状态。
        assert job_tasks.cleanup_store_task.run()["hours"] == 1
        assert job_tasks.cleanup_store_task.run()["hours"] == 1
        for _ in range(3):
            assert job_tasks.cleanup_store_task.run()["hours"] == 0
        with factory() as observer:
            assert set(
                observer.exec(
                    select(CampaignReportHour.campaign_id).where(
                        CampaignReportHour.tenant_id == seed.tenant_id
                    )
                ).all()
            ) == {"recent-0", "recent-1", "recent-2"}
    finally:
        redis_client.delete(cursor_key)
