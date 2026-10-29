"""校准只需同冻结分区的近期发布证据，不能汇总完整运行历史。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import event, text
from sqlmodel import select

from app.core.config import settings
from app.modules.ads.directory_calibration import recent_calibration
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.sync_models import AdDirectoryRun
from tests.modules.ads.test_directory_requests import _route
from tests.modules.reporting.sync_conftest import sync_seed as sync_seed
from tests.modules.reporting.test_sync_probe_indexes import _scanned


def test_recent_probe_stops_before_old_history(session, sync_seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        frozen_route=_route(sync_seed),
        advertiser_ids=["acc-1"],
        scope="directory",
        ad_types={"acc-1": {"REGULAR": ("campaign",)}},
    )
    run = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == sync_seed.tenant_id,
        )
    ).one()
    run.status = "COMPLETE"
    run.published_version = 1
    run.completed_at = now - timedelta(days=2)
    session.flush()
    session.connection().execute(
        text("""
        INSERT INTO ad_directory_run OVERRIDING USER VALUE
        SELECT (jsonb_populate_record(NULL::ad_directory_run,
          to_jsonb(r) || jsonb_build_object('id',gen_random_uuid(),
            'request_id',gen_random_uuid(),
            'completed_at',CASE WHEN i=2001 THEN CAST(:now AS timestamptz)
              ELSE r.completed_at END))).*
        FROM ad_directory_run r CROSS JOIN generate_series(2,2001) i
        WHERE r.id=:id
    """),
        {"id": run.id, "now": now},
    )
    captured = []
    connection = session.connection()

    def capture(_conn, _cursor, sql, params, _context, _many):
        if "ad_directory_run" in sql:
            captured.append((sql, params))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        result = recent_calibration(
            session,
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            bc_id="bc-a",
            actor_id=sync_seed.actor_id,
            frozen_route=_route(sync_seed),
            partition_key=run.partition_key,
            cutoff=now - timedelta(days=1),
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert result == now
    assert len(captured) == 1
    sql, params = captured[0]
    plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE, FORMAT JSON) " + sql,
        params,
    ).scalar_one()[0]["Plan"]
    assert _scanned(plan, "ad_directory_run") <= 3


def test_newer_failed_request_does_not_hide_older_recent_success(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        frozen_route=_route(sync_seed),
        advertiser_ids=["acc-1"],
        scope="directory",
        ad_types={"acc-1": {"REGULAR": ("campaign",)}},
    )
    older = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == sync_seed.tenant_id,
        )
    ).one()
    older.status, older.completed_at = "COMPLETE", now
    older.published_version = 1
    values = older.model_dump(exclude={"id", "request_id", "request_sequence"})
    values.update(status="FAILED", completed_at=None)
    newer = AdDirectoryRun(**values)
    session.add(newer)
    session.flush()
    assert newer.request_sequence > older.request_sequence
    args = {
        "tenant_id": sync_seed.tenant_id,
        "advertiser_id": "acc-1",
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "partition_key": older.partition_key,
        "cutoff": now - timedelta(days=1),
    }
    assert recent_calibration(session, **args) == now
    older.completed_at = now - timedelta(hours=24)
    session.flush()
    assert recent_calibration(session, **args) is None
