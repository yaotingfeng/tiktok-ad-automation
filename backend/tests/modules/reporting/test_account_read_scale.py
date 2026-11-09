"""生产同数量级合成账户池；计时只用于证据，回归以实际扫描对象和结果为准。"""

import json
from time import monotonic

from sqlalchemy import event, text

from app.modules.reporting.queries import _build_snapshot, _read_page
from tests.modules.reporting.test_queries import filters_for


def test_five_thousand_accounts_and_hundred_thousand_campaigns_have_bounded_snapshot(
    report_case, record_property
):
    case = report_case
    connection = case.session.connection()
    params = {"t": case.context.tenant_id, "bc": case.bc_id}
    connection.execute(
        text("""INSERT INTO advertiser_account
      (tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
      SELECT :t,'scale-'||i,'account-'||i,'USD','UTC','ENABLE',false FROM generate_series(1,5000)i"""),
        params,
    )
    connection.execute(
        text("""INSERT INTO bc_account_access
      (tenant_id,bc_id,advertiser_id,connection_id,in_bc,authorized,active,can_upload,can_build,permission_state)
      SELECT g.tenant_id,g.bc_id,'scale-'||i,g.connection_id,true,true,true,g.can_upload,g.can_build,g.permission_state
      FROM generate_series(1,5000)i CROSS JOIN bc_account_access g
      WHERE g.tenant_id=:t AND g.bc_id=:bc AND g.advertiser_id='report-account'"""),
        params,
    )
    started = monotonic()
    connection.execute(
        text("""INSERT INTO ad_object
      (tenant_id,advertiser_id,kind,remote_id,ad_type,name,configuration,observed_at,published_version)
      SELECT :t,'scale-'||a,'campaign','c-'||i,'REGULAR','campaign','{}',now(),1
      FROM generate_series(1,5000)a CROSS JOIN generate_series(1,20)i"""),
        params,
    )
    connection.execute(
        text("""INSERT INTO campaign_report_day
      (tenant_id,advertiser_id,stat_date,campaign_id,campaign_name,spend,currency,timezone,fetched_at,sync_job_id)
      SELECT :t,'scale-'||a,date '2026-09-30','c-'||i,'campaign',1,'USD','UTC',now(),gen_random_uuid()
      FROM generate_series(1,5000)a CROSS JOIN generate_series(1,20)i"""),
        params,
    )
    record_property("synthetic_publish_seconds", monotonic() - started)
    for table in (
        "account_report_day",
        "account_report_day_state",
        "account_membership_current",
        "bc_account_access",
        "advertiser_account",
    ):
        connection.execute(text(f"ANALYZE {table}"))
    captured = []

    def capture(_conn, _cursor, statement, values, _context, _many):
        if "INSERT INTO query_snapshot_row" in statement:
            captured.append((statement, values))

    event.listen(connection, "before_cursor_execute", capture)
    filters = filters_for("account", sort_by="spend", sort_direction="desc")
    started = monotonic()
    try:
        snapshot = _build_snapshot(
            case.session, context=case.context, bc_id=case.bc_id, filters=filters
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    record_property("snapshot_seconds", monotonic() - started)
    started = monotonic()
    page = _read_page(
        case.session,
        context=case.context,
        bc_id=case.bc_id,
        filters=filters,
        snapshot=snapshot,
        cursor=None,
        limit=50,
    )
    record_property("page_seconds", monotonic() - started)
    assert page.total == 5001
    assert page.summary["buckets"][0]["values"]["spend"] == "100000"
    assert len(page.items) == 50
    assert all(row.metric_buckets[0].values["spend"] == 20 for row in page.items)
    assert len(snapshot.naming_versions) == 5002
    assert (
        connection.execute(
            text(
                "SELECT sum(jsonb_array_length(refs)) FROM query_snapshot_row WHERE snapshot_id=:s"
            ),
            {"s": snapshot.id},
        ).scalar_one()
        == 0
    )
    # 再次EXPLAIN会写同主键，因此在回滚保存点里用另一随机快照ID捕获执行计划。
    statement, values = captured[0]
    from uuid import uuid4

    with case.session.begin_nested() as savepoint:
        values = dict(values, snapshot_id=uuid4())
        # 外键必须引用真实临时快照；复用当前父记录并只清掉当前诊断保存点内的行。
        values["snapshot_id"] = snapshot.id
        connection.execute(
            text("DELETE FROM query_snapshot_row WHERE snapshot_id=:s"),
            {"s": snapshot.id},
        )
        plan = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement, values
        ).scalar_one()[0]["Plan"]
        savepoint.rollback()

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    active = [node for node in nodes(plan) if node.get("Actual Loops", 0) > 0]
    relations = {node.get("Relation Name") for node in active}
    assert not relations & {
        "ad_object",
        "ad_entity_read",
        "campaign_name_current",
        "campaign_report_day",
        "account_membership_version",
    }
    facts = [
        node for node in active if node.get("Relation Name") == "account_report_day"
    ]
    assert (
        sum(
            (n["Actual Rows"] + n.get("Rows Removed by Filter", 0)) * n["Actual Loops"]
            for n in facts
        )
        <= 5000
    )
    record_property("snapshot_plan", json.dumps(plan))
