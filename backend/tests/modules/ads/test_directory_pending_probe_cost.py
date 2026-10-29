"""单户校准只判断该户在途，不重扫同授权下所有账户的积压。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, text

from app.modules.ads.directory_requests import directory_unblocked_accounts
from app.modules.ads.sync_models import AdDirectoryRun
from tests.modules.ads.test_directory_requests import _route
from tests.modules.reporting.sync_conftest import sync_seed as sync_seed
from tests.modules.reporting.test_sync_probe_indexes import _scanned


@pytest.mark.parametrize(
    "history,analyze_growth,batch_size",
    [(0, True, 1), (0, True, 2), (20000, True, 2), (20000, False, 16)],
)
def test_pending_probe_does_not_scan_other_accounts_history_or_backlog(
    session, sync_seed, history, analyze_growth, batch_size, record_property
):
    route = _route(sync_seed)
    run = AdDirectoryRun(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        connection_id=sync_seed.connection.id,
        frozen_route=route,
        partition_key="a" * 64,
        query={"advertiser_id": "acc-1", "include_deleted": True},
        status="QUEUED",
        claim_generation=1,
        kind="campaign",
        ad_type="SMART_PLUS",
    )
    session.add(run)
    session.flush()
    connection = session.connection()
    connection.execute(
        text("""
        INSERT INTO advertiser_account
        SELECT (jsonb_populate_record(NULL::advertiser_account,
          to_jsonb(a)||jsonb_build_object('advertiser_id','noise-'||i))).*
        FROM advertiser_account a CROSS JOIN generate_series(0,999) i
        WHERE a.tenant_id=:tenant AND a.advertiser_id='acc-1'
        """),
        {"tenant": sync_seed.tenant_id},
    )
    connection.execute(
        text("""
        INSERT INTO ad_directory_run OVERRIDING USER VALUE
        SELECT (jsonb_populate_record(NULL::ad_directory_run,
          to_jsonb(r) || jsonb_build_object(
            'id',gen_random_uuid(), 'request_id',gen_random_uuid(),
            'advertiser_id','noise-'||(i%1000),
            'query',r.query||jsonb_build_object('advertiser_id','noise-'||(i%1000)),
            'status',CASE WHEN i<=5000 THEN 'QUEUED' ELSE 'COMPLETE' END,
            'completed_at',CASE WHEN i>5000 THEN CAST(:now AS timestamptz) END
          ))).*
        FROM ad_directory_run r CROSS JOIN generate_series(1,25000) i
        WHERE r.id=:id
        """),
        {"id": run.id, "now": datetime.now(UTC)},
    )
    connection.execute(text("ANALYZE ad_directory_run"))
    if history:
        connection.execute(
            text("""
            INSERT INTO ad_directory_run OVERRIDING USER VALUE
            SELECT (jsonb_populate_record(NULL::ad_directory_run,
              to_jsonb(r)||jsonb_build_object('id',gen_random_uuid(),
                'request_id',gen_random_uuid(), 'advertiser_id','acc-2',
                'query',r.query||jsonb_build_object('advertiser_id','acc-2'),
                'status','COMPLETE','completed_at',CAST(:now AS timestamptz)))).*
            FROM ad_directory_run r CROSS JOIN generate_series(1,:n) i
            WHERE r.id=:id
            """),
            {"id": run.id, "now": datetime.now(UTC), "n": history},
        )
        if analyze_growth:
            connection.execute(text("ANALYZE ad_directory_run"))
    captured = []

    def capture(_conn, _cursor, sql, params, _context, _many):
        if "ad_directory_run" in sql:
            captured.append((sql, params))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        result = directory_unblocked_accounts(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            actor_id=sync_seed.actor_id,
            frozen_route=route,
            advertiser_ids=(
                ["acc-1", "acc-2"] + [f"noise-{i}" for i in range(batch_size - 2)]
            )[:batch_size],
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert result == ([] if batch_size == 1 else ["acc-2"])
    assert len(captured) == 1
    sql, params = captured[0]
    explanation = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params
    ).scalar_one()[0]
    plan = explanation["Plan"]
    record_property("plan", explanation)
    assert _scanned(plan, "ad_directory_run") <= max(4, batch_size * 5)
    assert plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0) < 512
    assert plan.get("Temp Written Blocks", 0) == 0
