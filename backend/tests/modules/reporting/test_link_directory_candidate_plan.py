"""真实候选入口：历史目录只扫描一次，保留冻结范围与ID成员语义。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text

from app.core.config import settings
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import request_link_directory
from app.modules.reporting.store_models import MaterialPostLink, ReportSyncJob
from tests.modules.reporting.test_backlog_control import request
from tests.modules.reporting.test_directory_refresh_pending_plan import (
    _prepare_actual_pending_query,
)
from tests.modules.reporting.test_jobs import _route
from tests.modules.reporting.test_material_query import D, material_row, publish
from tests.modules.reporting.test_sync_probe_indexes import _scanned


def _capture(session, job, now):
    captured = []

    class Captured(Exception):
        pass

    def capture(_conn, _cursor, sql, params, _context, _many):
        if "SELECT DISTINCT l.ad_remote_id" in sql:
            captured.append((sql, params))
            raise Captured

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        with pytest.raises(Captured):
            request_link_directory(session, job, now=now)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(captured) == 1
    return captured[0]


def _seed(session, seed, count=100):
    now = datetime.now(UTC)
    publish(
        session, seed, "acc-1", [material_row(f"mid-{i}", "1") for i in range(count)]
    )
    session.add_all(
        MaterialPostLink(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            main_material_id=f"mid-{i}",
            ad_remote_id=f"ad-{i % 20:02}",
            main_material_type="VIDEO_NON_SPARK_ADS",
            observed_at=now,
        )
        for i in range(count)
    )
    identity = request(session, seed, D)
    session.flush()
    return session.get(ReportSyncJob, identity), now


def _run(seed, now, **changes):
    fields = {
        "tenant_id": seed.tenant_id,
        "advertiser_id": "acc-1",
        "bc_id": "bc-a",
        "actor_id": seed.actor_id,
        "connection_id": seed.connection.id,
        "frozen_route": _route(seed),
        "partition_key": "a" * 64,
        "query": {"ids": ["ad-00"], "include_deleted": True},
        "status": "COMPLETE",
        "kind": "ad",
        "ad_type": "SMART_PLUS",
        "created_at": now - timedelta(days=1),
    }
    return AdDirectoryRun(**(fields | changes))


@pytest.mark.parametrize("count", [100, 500, 2000])
@pytest.mark.parametrize("mode", ["force_custom_plan", "force_generic_plan"])
def test_candidate_history_scan_does_not_repeat_per_mapped_post(
    session, sync_seed, monkeypatch, record_property, mode, count
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, now = _seed(session, sync_seed, count=count)
    old = _run(sync_seed, now)
    recent = _run(sync_seed, now, created_at=now - timedelta(minutes=1))
    session.add_all([old, recent])
    session.flush()
    connection = session.connection()
    connection.execute(
        text("""
      INSERT INTO ad_directory_run OVERRIDING USER VALUE
      SELECT (jsonb_populate_record(NULL::ad_directory_run,to_jsonb(r)||jsonb_build_object(
        'id',gen_random_uuid(),'request_id',gen_random_uuid(),
        'query',jsonb_build_object('ids',jsonb_build_array('old-'||i),'include_deleted',true,'padding',repeat(md5(i::text),16))))).*
      FROM ad_directory_run r CROSS JOIN generate_series(1,20000) i WHERE r.id=:id
    """),
        {"id": old.id},
    )
    for table in (
        "ad_directory_run",
        "material_report_day",
        "material_post_link",
        "ad_object",
        "ad_material_reference",
    ):
        connection.execute(text(f"ANALYZE {table}"))
    captured = _capture(session, job, now)
    connection.execute(text(f"SET LOCAL plan_cache_mode={mode}"))
    name, execute = _prepare_actual_pending_query(connection, *captured)
    try:
        rows = connection.exec_driver_sql(execute).mappings().all()
        assert {row["ad_remote_id"] for row in rows} == {
            f"ad-{i:02}" for i in range(1, 20)
        }
        explanation = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + execute
        ).scalar_one()[0]
        record_property("plan", explanation)
        scanned = _scanned(explanation["Plan"], "ad_directory_run")
        record_property("directory_rows_examined", scanned)
        assert scanned <= 20100

    finally:
        connection.exec_driver_sql(f"DEALLOCATE {name}")


@pytest.mark.parametrize(
    "case,blocked",
    [
        ("QUEUED", True),
        ("RUNNING", True),
        ("ADMISSION_WAIT", True),
        ("WAITING_REMOTE", True),
        ("COMPLETE", False),
        ("FAILED", False),
        ("CANCELLED", False),
        ("STALE", False),
        ("recent_COMPLETE", True),
        ("recent_FAILED", True),
        ("recent_CANCELLED", True),
        ("recent_STALE", True),
        ("cutoff", True),
        ("expired", False),
        ("bc", False),
        ("actor", False),
        ("route", False),
        ("account", False),
        ("tenant", False),
        ("kind", False),
        ("type", False),
        ("background_for_live", False),
        ("live_for_background", True),
        ("include_false", False),
        ("include_string", True),
        ("parent_only", False),
        ("ids_object", True),
        ("ids_scalar", True),
        ("ids_number", False),
        ("ids_null", False),
    ],
)
def test_blocking_candidates_keep_original_scope_and_json_membership(
    session, sync_seed, monkeypatch, case, blocked
):
    from inspect import unwrap

    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, now = _seed(session, sync_seed, count=1)
    fields = {"status": "QUEUED"}
    if case in {
        "QUEUED",
        "RUNNING",
        "ADMISSION_WAIT",
        "WAITING_REMOTE",
        "COMPLETE",
        "FAILED",
        "CANCELLED",
        "STALE",
    }:
        fields["status"] = case
    elif case.startswith("recent_"):
        fields.update(
            status=case.removeprefix("recent_"), created_at=now - timedelta(minutes=1)
        )
    elif case in {"cutoff", "expired"}:
        fields.update(
            status="COMPLETE",
            created_at=now - timedelta(minutes=10, microseconds=case == "expired"),
        )
    elif case == "bc":
        fields["bc_id"] = "bc-b"
        fields["frozen_route"] = _route(sync_seed) | {"bc_id": "bc-b"}
    elif case == "route":
        fields["frozen_route"] = _route(sync_seed) | {"binding_revision": 99}
    elif case == "account":
        fields["advertiser_id"] = "acc-2"
    elif case in {"actor", "tenant"}:
        other = unwrap(seed_fixture)(session)
        fields["actor_id"] = other.actor_id
        if case == "tenant":
            fields.update(
                tenant_id=other.tenant_id,
                connection_id=other.connection.id,
                frozen_route=_route(other),
            )
    elif case == "kind":
        fields["kind"] = "campaign"
    elif case == "type":
        fields["ad_type"] = "REGULAR"
    elif case == "background_for_live":
        now = datetime(D.year, D.month, D.day, 12, tzinfo=UTC)
        fields["workload"] = "background"
    elif case == "live_for_background":
        fields["workload"] = "live"
    else:
        query = {"ids": ["ad-00"], "include_deleted": True}
        if case == "include_false":
            query["include_deleted"] = False
        elif case == "include_string":
            query["include_deleted"] = "true"
        elif case == "parent_only":
            query = {"parent_ids": ["ad-00"], "include_deleted": True}
        elif case == "ids_object":
            query["ids"] = {"ad-00": False}
        elif case == "ids_scalar":
            query["ids"] = "ad-00"
        elif case == "ids_number":
            query["ids"] = [0]
        elif case == "ids_null":
            query["ids"] = None
        fields["query"] = query
    session.add(_run(sync_seed, now, **fields))
    session.flush()
    connection = session.connection()
    name, execute = _prepare_actual_pending_query(
        connection, *_capture(session, job, now)
    )
    try:
        rows = connection.exec_driver_sql(execute).mappings().all()
        assert [r["ad_remote_id"] for r in rows] == ([] if blocked else ["ad-00"])
    finally:
        connection.exec_driver_sql(f"DEALLOCATE {name}")


@pytest.mark.parametrize(
    "spark,bridge,complete,expected",
    [
        (True, False, True, False),
        (True, False, False, True),
        (True, True, True, True),
        (False, False, True, True),
    ],
)
def test_spark_complete_directory_guard_is_unchanged(
    session, sync_seed, monkeypatch, spark, bridge, complete, expected
):
    from sqlmodel import select

    from app.modules.ads.models import AdMaterialReference, AdObject

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, now = _seed(session, sync_seed, count=1)
    link = session.exec(
        select(MaterialPostLink).where(
            MaterialPostLink.tenant_id == sync_seed.tenant_id
        )
    ).one()
    link.main_material_type = "VIDEO_SPARK_ADS" if spark else "VIDEO_NON_SPARK_ADS"
    link.ad_material_id = "bridge" if bridge else None
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="ad",
            remote_id="ad-00",
            ad_type="SMART_PLUS",
            observed_at=now,
            published_version=1,
        )
    )
    session.add(
        AdMaterialReference(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            ad_remote_id="ad-00",
            ad_material_id="other",
            platform_material_id="vid",
            material_type="VIDEO",
            complete=complete,
            published_version=1,
        )
    )
    session.flush()
    connection = session.connection()
    name, execute = _prepare_actual_pending_query(
        connection, *_capture(session, job, now)
    )
    try:
        assert bool(connection.exec_driver_sql(execute).mappings().all()) == expected
    finally:
        connection.exec_driver_sql(f"DEALLOCATE {name}")
