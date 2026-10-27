"""事实账户筛选应按账户短探测，不扫描账户下整天明细。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import event, insert, text

from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting import planner
from app.modules.reporting.store_models import AdReportDay, AdSyncBc, MaterialReportDay


def test_known_accounts_probe_one_fact_per_account(session, sync_seed):
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    accounts = [f"probe-{index:04d}" for index in range(5000)]
    session.execute(
        insert(AdvertiserAccount),
        [
            {
                "tenant_id": sync_seed.tenant_id,
                "advertiser_id": account,
                "timezone": "UTC",
            }
            for account in accounts
        ],
    )
    common = {
        "tenant_id": sync_seed.tenant_id,
        "stat_date": now.date(),
        "currency": "USD",
        "timezone": "UTC",
        "fetched_at": now,
        "sync_job_id": uuid4(),
        "spend": 0,
    }
    for model in (AdReportDay, MaterialReportDay):
        # 奇数账户仅有素材事实；偶数两表都有，仍只需找到第一条广告事实。
        rows = []
        for index, account in enumerate(accounts):
            if model is AdReportDay and index % 2:
                continue
            for item in range(4):
                identity = f"fact-{item:04d}"
                fields = (
                    {"ad_id": identity, "adgroup_id": "g", "campaign_id": "c"}
                    if model is AdReportDay
                    else {"main_material_id": identity, "main_material_type": "VIDEO"}
                )
                rows.append(common | {"advertiser_id": account} | fields)
        session.execute(insert(model), rows)
        session.execute(text(f"ANALYZE {model.__tablename__}"))
    session.execute(text("ANALYZE advertiser_account"))
    queries = []

    def capture(_conn, _cursor, sql, params, _context, _many):
        if "FROM ad_report_day r" in sql and "FROM material_report_day r" in sql:
            queries.append((sql, params))

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert planner.known_reporting_accounts(
            session, plan, accounts=accounts, now=now
        ) == set(accounts)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(queries) == 1
    sql, params = queries[0]
    result = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params
    ).scalar_one()

    def fact_rows(node):
        count = 0
        if node.get("Relation Name", "").startswith(
            ("ad_report_day", "material_report_day")
        ):
            count = (
                node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)
            ) * node.get("Actual Loops", 1)
        return count + sum(fact_rows(child) for child in node.get("Plans", []))

    assert fact_rows(result[0]["Plan"]) <= len(accounts) * 2, result
