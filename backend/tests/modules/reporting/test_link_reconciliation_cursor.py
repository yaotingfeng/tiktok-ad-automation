"""归并维护必须跨轮推进，数据库提交之前不能移动 Redis 游标。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.core.db import engine
from app.modules.reporting import link_work
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_link_maintenance_budget import waiting


def test_marked_page_advances_to_unmarked_tail(committed_sync_seed, redis_client):
    from app.modules.reporting.link_reconciliation import reconcile_link_work_batch

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        for i in range(55):
            waiting(session, seed, now, i, id=UUID(int=i + 1))
        target = waiting(session, seed, now, 55, id=UUID(int=56), progress={}).id
        session.commit()
    reconcile_link_work_batch(
        database_engine=engine, redis_client=redis_client, now=now
    )
    with factory() as session:
        assert "coalesced_v1" not in session.get(ReportSyncJob, target).progress
    reconcile_link_work_batch(
        database_engine=engine, redis_client=redis_client, now=now
    )
    with factory() as session:
        assert session.get(ReportSyncJob, target).status == "CANCELLED"


def test_unmergeable_prefix_and_leased_row_return_after_wrap(
    committed_sync_seed, redis_client
):
    from app.modules.reporting.link_reconciliation import reconcile_link_work_batch

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        first = waiting(
            session,
            seed,
            now,
            id=UUID(int=1),
            progress={},
            claimed_until=now + timedelta(minutes=1),
        ).id
        tail = waiting(
            session, seed, now, 1, id=UUID(int=2), progress={}, advertiser_ids=["other"]
        ).id
        session.commit()
    for _ in range(2):
        reconcile_link_work_batch(
            database_engine=engine, redis_client=redis_client, now=now, limit=1
        )
    with factory() as session:
        assert "coalesced_v1" not in session.get(ReportSyncJob, first).progress
        assert session.get(ReportSyncJob, tail).progress["coalesced_v1"] is True
        session.get(ReportSyncJob, first).claimed_until = None
        session.commit()
    for _ in range(2):
        reconcile_link_work_batch(
            database_engine=engine, redis_client=redis_client, now=now, limit=1
        )
    with factory() as session:
        assert session.get(ReportSyncJob, first).progress["coalesced_v1"] is True


def test_database_failure_keeps_redis_position(
    committed_sync_seed, redis_client, monkeypatch
):
    from app.modules.reporting import link_reconciliation as runner

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        waiting(session, seed, now, progress={})
        session.commit()

    def fail(session, **kwargs):
        kwargs["scan"]["cursor"] = ["must-not-save"]
        session.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(link_work, "reconcile_link_work", fail)
    with pytest.raises(DBAPIError):
        runner.reconcile_link_work_batch(
            database_engine=engine, redis_client=redis_client, now=now
        )
    assert redis_client.get(runner.CURSOR_KEY) is None


def test_concurrent_runner_does_not_read_or_advance_cursor(redis_client, monkeypatch):
    from app.modules.reporting import link_reconciliation as runner

    with redis_client.lock(runner.LOCK_KEY, timeout=30):
        monkeypatch.setattr(
            link_work, "reconcile_link_work", lambda *a, **k: pytest.fail("must skip")
        )
        assert (
            runner.reconcile_link_work_batch(
                database_engine=engine, redis_client=redis_client, now=datetime.now(UTC)
            )
            == 0
        )
    assert redis_client.get(runner.CURSOR_KEY) is None


@pytest.fixture(autouse=True)
def isolated_cursor(redis_client):
    from app.modules.reporting.link_reconciliation import CURSOR_KEY, LOCK_KEY

    redis_client.delete(CURSOR_KEY, LOCK_KEY)
    yield
    redis_client.delete(CURSOR_KEY, LOCK_KEY)


def test_large_active_backlog_only_decodes_bounded_page(session, sync_seed):
    now = datetime.now(UTC)
    job = waiting(session, sync_seed, now)
    # 不可高度压缩的 progress 模拟冷 TOAST；历史与活动数量都不能放大每页 JSON 读取。
    session.execute(
        text("""
        INSERT INTO report_sync_job
          (id,tenant_id,bc_id,kind,priority,status,advertiser_ids,campaign_ids,
           start_date,end_date,frozen_route,actor_id,job_key,due_at,attempts,
           request_count,progress,created_at)
        SELECT gen_random_uuid(),tenant_id,bc_id,kind,priority,status,
          advertiser_ids,campaign_ids,start_date,end_date,frozen_route,actor_id,
          'active-' || g,due_at,attempts,request_count,
          jsonb_build_object('coalesced_v1',true,'evidence',
            (SELECT string_agg(md5(n::text),'') FROM generate_series(1,200) n)),created_at
        FROM report_sync_job CROSS JOIN generate_series(1,5000) g WHERE id=:id
    """),
        {"id": job.id},
    )
    session.execute(text("ANALYZE report_sync_job"))
    sql = link_work.RECONCILE_CANDIDATES_SQL.format(after="")
    result = session.execute(
        text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql), {"limit": 50}
    ).scalar_one()[0]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    parts = list(nodes(result["Plan"]))
    page = next(n for n in parts if n.get("Subplan Name") == "CTE active_links")
    assert page["Actual Rows"] == 50
    progress = next(n for n in parts if n.get("Subplan Name") == "SubPlan 2")
    assert progress["Actual Loops"] == 50
    assert progress["Index Name"] == "report_sync_job_pkey"
    # 全活动且同一账户时 PG 可选择顺扫轻量排序键；不能为测试强制索引。
    # 重型 progress 读取仍严格在有限页之后，旧查询会读所有 5001 个 JSON。


def test_partial_deadline_does_not_jump_unexamined_tail(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    first = waiting(session, sync_seed, now, id=UUID(int=1), progress={})
    second = waiting(session, sync_seed, now, 1, id=UUID(int=2), progress={})
    clock = [now]

    class Clock:
        @staticmethod
        def now(_tz):
            return clock[0]

    seen = []

    def reconcile(_session, candidate_id, **_):
        seen.append(candidate_id)
        clock[0] += timedelta(seconds=2)
        return 0

    monkeypatch.setattr(link_work, "datetime", Clock)
    monkeypatch.setattr(link_work, "_reconcile_candidate", reconcile)
    scan = {}
    link_work.reconcile_link_work(
        session, now=now, deadline=now + timedelta(seconds=1), scan=scan
    )
    assert scan["cursor"][-1] == str(first.id)
    link_work.reconcile_link_work(session, now=now, scan=scan)
    assert seen == [first.id, second.id]


def test_expected_timeout_rotates_but_unexpected_errors_propagate(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    first = waiting(session, sync_seed, now, id=UUID(int=1), progress={})
    second = waiting(session, sync_seed, now, 1, id=UUID(int=2), progress={})
    scan = {}

    def reconcile(db, candidate_id, **_):
        if candidate_id == first.id:
            db.execute(text("SELECT pg_sleep(0.1)"))
        return 0

    monkeypatch.setattr(link_work, "_reconcile_candidate", reconcile)
    session.execute(text("SET LOCAL statement_timeout='30ms'"))
    assert link_work.reconcile_link_work(session, now=now, scan=scan) == 0
    assert scan["cursor"][-1] == str(second.id)
    scan.clear()

    def unexpected(db, *_args, **_kwargs):
        db.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(link_work, "_reconcile_candidate", unexpected)
    with pytest.raises(DBAPIError, match="division by zero"):
        link_work.reconcile_link_work(session, now=now, scan=scan)
    assert "cursor" not in scan


def test_redis_failure_after_commit_is_safe_to_rescan(
    committed_sync_seed, redis_client, monkeypatch
):
    from redis.exceptions import ConnectionError

    from app.modules.reporting import link_reconciliation as runner

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        target = waiting(session, seed, now, progress={}).id
        session.commit()
    original = redis_client.set

    def fail_cursor(name, *args, **kwargs):
        if name == runner.CURSOR_KEY:
            raise ConnectionError("local test disconnect")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(redis_client, "set", fail_cursor)
        with pytest.raises(ConnectionError):
            runner.reconcile_link_work_batch(
                database_engine=engine, redis_client=redis_client, now=now
            )
    with factory() as session:
        assert session.get(ReportSyncJob, target).progress["coalesced_v1"] is True
    assert redis_client.get(runner.CURSOR_KEY) is None
    assert (
        runner.reconcile_link_work_batch(
            database_engine=engine, redis_client=redis_client, now=now
        )
        == 0
    )


def test_unmergeable_front_page_does_not_starve_tail(committed_sync_seed, redis_client):
    from app.modules.reporting.link_reconciliation import reconcile_link_work_batch

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        for i in range(55):
            waiting(session, seed, now, i, advertiser_ids=["a", "b"], progress={})
        target = waiting(session, seed, now, 55, advertiser_ids=["z"], progress={}).id
        session.commit()
    for _ in range(2):
        reconcile_link_work_batch(
            database_engine=engine, redis_client=redis_client, now=now
        )
    with factory() as session:
        assert session.get(ReportSyncJob, target).progress["coalesced_v1"] is True


def test_commit_failure_rolls_back_work_without_moving_cursor(
    committed_sync_seed, redis_client
):
    from app.modules.reporting import link_reconciliation as runner

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session:
        target = waiting(session, seed, now, progress={}).id
        session.commit()

    def reject_commit(session):
        if not session.in_nested_transaction():
            raise RuntimeError("test commit rejected")

    event.listen(runner.Session, "before_commit", reject_commit)
    try:
        with pytest.raises(RuntimeError, match="test commit rejected"):
            runner.reconcile_link_work_batch(
                database_engine=engine, redis_client=redis_client, now=now
            )
    finally:
        event.remove(runner.Session, "before_commit", reject_commit)
    assert redis_client.get(runner.CURSOR_KEY) is None
    with factory() as session:
        assert "coalesced_v1" not in session.get(ReportSyncJob, target).progress
