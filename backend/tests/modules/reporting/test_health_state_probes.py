"""健康水位合并探测：保留账户当地今日/昨日和空水位，减少重复主键访问。"""

from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import event, text

from app.modules.reporting.health import measure_health

REFERENCE = """
WITH active AS (
  SELECT advertiser_id,timezone,(:now AT TIME ZONE timezone)::date local_day
  FROM advertiser_account WHERE tenant_id=:t AND advertiser_id=ANY(:ids)
)
SELECT a.advertiser_id,a.timezone,a.local_day,k.kind report_kind,
  s.last_success_at,previous.last_success_at previous_success_at
FROM active a CROSS JOIN (VALUES ('ad_day'),('material_day')) k(kind)
LEFT JOIN report_sync_state s ON s.tenant_id=:t AND s.advertiser_id=a.advertiser_id
  AND s.stat_date=a.local_day AND s.report_kind=k.kind
LEFT JOIN report_sync_state previous ON previous.tenant_id=:t
  AND previous.advertiser_id=a.advertiser_id AND previous.stat_date=a.local_day-1
  AND previous.report_kind=k.kind
"""


def _state_work(node):
    visits = rows = 0
    if node.get("Relation Name") == "report_sync_state":
        visits = node.get("Actual Loops", 1)
        rows = visits * (
            node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)
        )
    for child in node.get("Plans", []):
        more_visits, more_rows = _state_work(child)
        visits += more_visits
        rows += more_rows
    return visits, rows


def test_health_states_use_one_bounded_probe_per_account(session, sync_seed):
    now = datetime(2026, 10, 27, 0, 5, tzinfo=UTC)
    accounts = [f"state-{i:04d}" for i in range(1000)]
    session.execute(
        text("""
        INSERT INTO advertiser_account
          (tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
        SELECT :tenant,'state-'||lpad(i::text,4,'0'),'','USD',
          CASE i%3 WHEN 0 THEN 'UTC' WHEN 1 THEN 'Pacific/Kiritimati' ELSE 'America/Adak' END,
          'ENABLE',false FROM generate_series(0,999) i
        """),
        {"tenant": sync_seed.tenant_id},
    )
    session.execute(
        text("""
        INSERT INTO report_sync_state
          (tenant_id,advertiser_id,report_kind,stat_date,status,row_count,last_success_at,updated_at)
        SELECT :tenant,'state-'||lpad(i::text,4,'0'),k.kind,
          date '2026-10-27'-d,'COMPLETE',0,
          CASE WHEN i%13=0 THEN NULL ELSE CAST(:now AS timestamptz)-interval '1 minute'*(i%40) END,
          :now
        FROM generate_series(0,999) i CROSS JOIN generate_series(-1,40) d
        CROSS JOIN (VALUES ('ad_day'),('material_day')) k(kind)
        WHERE (i+d)%17<>0
        """),
        {"tenant": sync_seed.tenant_id, "now": now},
    )
    session.execute(text("ANALYZE advertiser_account"))
    session.execute(text("ANALYZE report_sync_state"))
    plan = SimpleNamespace(
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        last_active_advertiser_ids=accounts,
        active_screened_at=now,
    )
    queries = []

    def capture(_conn, _cursor, sql, params, _context, _many):
        if "WITH active AS" in sql:
            queries.append((sql, params))

    conn = session.connection()
    event.listen(conn, "before_cursor_execute", capture)
    try:
        measure_health(session, plan, now=now, accounts=accounts)
    finally:
        event.remove(conn, "before_cursor_execute", capture)
    assert len(queries) == 1
    sql, params = queries[0]
    actual = conn.exec_driver_sql(sql, params).all()
    expected = session.execute(
        text(REFERENCE), {"now": now, "t": sync_seed.tenant_id, "ids": accounts}
    ).all()
    assert sorted(actual) == sorted(expected)
    explain = conn.exec_driver_sql(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params
    ).scalar_one()[0]
    visits, rows = _state_work(explain["Plan"])
    assert rows <= 4 * len(accounts), explain
    assert visits <= len(accounts), explain
