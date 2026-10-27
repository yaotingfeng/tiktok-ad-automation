"""同一账户共享父目录只检查一次，仍逐广告保留完整缺口。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import event, insert, text

from app.modules.ads.models import AdObject
from app.modules.reporting.health import _fresh_parents, measure_health
from app.modules.reporting.store_models import AdReportDay


def _object_visits(node):
    count = (
        node.get("Actual Loops", 0) if node.get("Relation Name") == "ad_object" else 0
    )
    return count + sum(_object_visits(child) for child in node.get("Plans", []))


def test_shared_parents_are_probed_once_and_missing_ads_are_not_collapsed(
    session, sync_seed
):
    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    count = 2000
    plan = SimpleNamespace(
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        last_active_advertiser_ids=["acc-2"],
        active_screened_at=now,
    )
    session.execute(
        insert(AdReportDay),
        [
            {
                "tenant_id": plan.tenant_id,
                "advertiser_id": "acc-2",
                "stat_date": now.date(),
                "currency": "USD",
                "timezone": "UTC",
                "fetched_at": now,
                "sync_job_id": uuid4(),
                "spend": 1,
                "ad_id": f"ad-{i}",
                "adgroup_id": "group",
                "campaign_id": "missing-campaign",
            }
            for i in range(count)
        ],
    )
    session.execute(
        insert(AdObject),
        [
            {
                "tenant_id": plan.tenant_id,
                "advertiser_id": "acc-2",
                "kind": kind,
                "remote_id": remote,
                "ad_type": "NORMAL",
                "observed_at": now,
                "published_version": 1,
                "configuration": {},
            }
            for kind, remote in [("ad", f"ad-{i}") for i in range(count)]
            + [("adgroup", "group")]
        ],
    )
    session.execute(text("ANALYZE ad_object"))
    session.execute(text("ANALYZE ad_report_day"))
    # 不依赖规划器偶然插入 Memoize 才控制共享父层的读取量。
    session.execute(text("SET LOCAL enable_memoize=off"))
    queries = []

    def capture(_conn, _cursor, sql, params, _context, _many):
        if "FROM ad_object" in sql:
            queries.append((sql, params))

    conn = session.connection()
    event.listen(conn, "before_cursor_execute", capture)
    try:
        metrics, _, repairs = measure_health(session, plan, now=now, accounts=["acc-2"])
    finally:
        event.remove(conn, "before_cursor_execute", capture)
    assert metrics["missing_directory_ads"] == count
    assert repairs["ad"] == {"acc-2"}
    assert len(queries) == 3
    visits = 0
    for sql, params in queries:
        result = conn.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params
        ).scalar_one()[0]
        visits += _object_visits(result["Plan"])
    assert visits <= count + 2


def test_null_parent_identity_stays_missing_without_breaking_sorted_probes(
    session, sync_seed
):
    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-2",
            kind="adgroup",
            remote_id="group",
            ad_type="NORMAL",
            observed_at=now,
            published_version=1,
        )
    )
    session.flush()
    fresh = _fresh_parents(
        session,
        {
            "t": sync_seed.tenant_id,
            "strict_accounts": [],
            "rule_cutoff": now,
            "parent_cutoff": now,
        },
        kind="adgroup",
        keys={("acc-2", None), ("acc-2", "group")},
    )
    assert fresh == {("acc-2", "group")}
    assert ("acc-2", None) not in fresh
