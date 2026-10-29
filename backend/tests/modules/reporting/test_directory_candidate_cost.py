"""派发成本按数据库实际读取约束，锁住队首仍须补满可用窗口。"""

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, text
from sqlmodel import select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.ads import directory_dispatch
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from tests.modules.reporting.test_directory_direct_delivery import directory
from tests.modules.reporting.test_jobs import _route


def test_locked_first_candidate_does_not_leave_a_free_slot(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(directory_dispatch, "TARGETED_DISPATCH_WINDOW", 1)
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        first = directory(db, seed)
        first.created_at = now - timedelta(minutes=2)
        first_id = first.id
        second_id = directory(db, seed).id
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(AdDirectoryRun)
            .where(AdDirectoryRun.id == first_id)
            .with_for_update()
        ).one()
        with factory() as db, db.begin():
            assert job_tasks._dispatch_directories(db, now) == 1
            message = db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id
                )
            ).one()
            assert message.payload["run_id"] == str(second_id)


def _visited(plan, relation):
    own = 0
    if plan.get("Relation Name") == relation:
        own = (
            plan.get("Actual Rows", 0) + plan.get("Rows Removed by Filter", 0)
        ) * plan.get("Actual Loops", 1)
    return own + sum(_visited(child, relation) for child in plan.get("Plans", []))


def test_locked_dependency_heads_preserve_background_reservations(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(directory_dispatch, "TARGETED_DISPATCH_WINDOW", 0)
    now = datetime.now(UTC)
    dependency_ids, full_ids = [], []
    with factory() as db, db.begin():
        for index in range(12):
            dependency = directory(db, seed)
            dependency.workload = "background"
            dependency.created_at = now - timedelta(minutes=30 - index)
            dependency_ids.append(dependency.id)
            full = directory(db, seed)
            full.workload = "background"
            full.query = dict(full.query, ids=[])
            full.created_at = now - timedelta(minutes=30 - index)
            full_ids.append(full.id)
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(AdDirectoryRun)
            .where(AdDirectoryRun.id.in_(dependency_ids[:4]))
            .with_for_update()
        ).all()
        with factory() as db, db.begin():
            assert job_tasks._dispatch_directories(db, now) == 8
            dispatched = {
                row.payload["run_id"]
                for row in db.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == seed.tenant_id
                    )
                ).all()
            }
            assert dispatched == {
                str(value) for value in dependency_ids[4:8] + full_ids[:4]
            }


def test_large_live_lane_does_not_starve_same_tenants_background(
    session, sync_seed, monkeypatch
):
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(directory_dispatch, "TARGETED_DISPATCH_WINDOW", 1)
    other = seed_fixture.__wrapped__(session)
    now = datetime.now(UTC)
    for _ in range(1000):
        run = directory(session, sync_seed)
        run.created_at = now - timedelta(hours=1)
    for seed in (sync_seed, other):
        for index in range(36):
            run = directory(session, seed)
            run.workload = "background"
            run.created_at = now - timedelta(minutes=30 - index / 2)
    for turn in range(3):
        assert (
            job_tasks._dispatch_directories(session, now + timedelta(seconds=turn)) == 9
        )
        messages = session.exec(select(PendingDispatch)).all()
        active = []
        for message in messages:
            run = session.get(AdDirectoryRun, message.payload["run_id"])
            if run.status == "QUEUED":
                active.append(run)
        # B 每轮仍有背景积压；A 的巨大实时尾部不能挡住 A 的背景服务。
        assert any(
            run.workload == "background" and run.tenant_id == sync_seed.tenant_id
            for run in active
        )
        assert any(
            run.workload == "background" and run.tenant_id == other.tenant_id
            for run in active
        )
        for run in active:
            run.status = "COMPLETE"
        session.flush()


def test_large_backlog_is_not_rescanned_for_each_ranking_and_capacity(
    session, sync_seed, monkeypatch, record_property
):
    """30k pending + 90k historical messages: one active-row extraction per refill."""
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    count = 30_000
    params = {
        "tenant": sync_seed.tenant_id,
        "actor": sync_seed.actor_id,
        "connection": sync_seed.connection.id,
        "route": json.dumps(_route(sync_seed)),
        "now": now,
        "count": count,
    }
    session.connection().execute(
        text("""
        INSERT INTO ad_directory_run
          (id, tenant_id, advertiser_id, bc_id, actor_id, connection_id, channel,
           frozen_route, request_id, partition_key, query, status, claim_generation,
           next_page, coverage, created_at, kind, ad_type, workload)
        SELECT gen_random_uuid(), :tenant, 'acc-1', 'bc-a', :actor, :connection,
          'OFFICIAL_API', CAST(:route AS jsonb), gen_random_uuid(),
          lpad(i::text,64,'0'), jsonb_build_object('ids',jsonb_build_array(i::text)),
          'QUEUED',1,1,'PENDING',:now,'ad','SMART_PLUS',
          CASE WHEN i%2=0 THEN 'live' ELSE 'background' END
        FROM generate_series(1,:count) i
    """),
        params,
    )
    session.connection().execute(
        text("""
        INSERT INTO pending_dispatch
          (id,tenant_id,actor_id,task_name,task_key,payload,available_at,published_at,attempts)
        SELECT gen_random_uuid(),:tenant,:actor,'ads.targeted_step',
          'old-directory-message:'||i,'{}'::jsonb,:now,:now,1
        FROM generate_series(1,:count*3) i
    """),
        params,
    )
    session.connection().execute(text("ANALYZE ad_directory_run"))
    session.connection().execute(text("ANALYZE pending_dispatch"))
    plans = []

    def capture(_conn, cursor, sql, parameters, _context, _many):
        if (
            sql.lstrip().upper().startswith(("SELECT", "WITH"))
            and "ad_directory_run" in sql
        ):
            # 在派发前测量，避免窗口已满时漏测候选锁行分支。
            cursor.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql, parameters)
            plans.append(cursor.fetchone()[0][0])

    conn = session.connection()
    event.listen(conn, "before_cursor_execute", capture)
    try:
        assert job_tasks._dispatch_directories(session, now) == 40
    finally:
        event.remove(conn, "before_cursor_execute", capture)
    visits = sum(_visited(p["Plan"], "ad_directory_run") for p in plans)
    record_property("directory_dispatch_plans", json.dumps(plans))
    record_property("directory_run_visits", visits)
    logging.getLogger(__name__).info(
        "directory dispatch cost %s",
        json.dumps(
            {
                "run_visits": visits,
                "pending": count,
                "query_ms": [round(p["Execution Time"], 2) for p in plans],
            }
        ),
    )
    # A small allowance covers bounded primary-key rechecks; it cannot hide
    # another full pending scan for capacity or a ranking layer.
    assert visits <= count + 1000
