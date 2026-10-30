"""真实 PG 检查布尔补链查询；成本断言看实际工作量，不依赖机器耗时。"""

import json
import re
from datetime import timedelta

import pytest
from psycopg import sql as pgsql
from sqlalchemy import event, text

from app.modules.reporting import material_links
from tests.modules.reporting.test_material_query import D
from tests.modules.reporting.test_material_value import fact


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def _explain_prepared(connection, statement, parameters, mode):
    # 捕获实际应用 SQL，再显式 PREPARE，确保 generic 不是只设置 GUC 的假覆盖。
    names = list(dict.fromkeys(re.findall(r"%\((\w+)\)s", statement)))
    prepared = re.sub(
        r"%\((\w+)\)s", lambda match: f"${names.index(match[1]) + 1}", statement
    )
    connection.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
    raw = connection.connection.driver_connection
    with raw.cursor() as cursor:
        cursor.execute("PREPARE link_existence_cost AS " + prepared)
        try:
            cursor.execute(
                pgsql.SQL(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE link_existence_cost ({})"
                ).format(
                    pgsql.SQL(",").join(
                        pgsql.Literal(parameters[name]) for name in names
                    )
                )
            )
            return cursor.fetchone()[0][0]
        finally:
            cursor.execute("DEALLOCATE link_existence_cost")


@pytest.mark.parametrize("mode", ["force_custom_plan", "force_generic_plan"])
@pytest.mark.parametrize("missing_count", [2000, 1, 0])
def test_needs_returns_one_row_and_stops_mapping_probe(
    session, sync_seed, mode, missing_count, record_property
):
    connection = session.connection()
    params = {"tenant": sync_seed.tenant_id, "day": D, "mapped": 4000 - missing_count}
    # 40k 窗口事实 + 100k 窗口外事实、102k 映射；覆盖常见/稀疏/无缺口。
    connection.execute(
        text("""
        INSERT INTO material_report_day
          (tenant_id,advertiser_id,stat_date,main_material_id,main_material_type,
           currency,timezone,fetched_at,sync_job_id,spend,revenue_total)
        SELECT :tenant,'acc-1',CAST(:day AS date)-d,'post-'||i,'VIDEO_SPARK_ADS',
          'USD','UTC',now(),gen_random_uuid(),1,0
        FROM generate_series(1,4000) i CROSS JOIN generate_series(0,9) d
        """),
        params,
    )
    connection.execute(
        text("""
        INSERT INTO material_post_link
          (tenant_id,advertiser_id,main_material_id,ad_remote_id,main_material_type,
           platform_material_id,observed_at)
        SELECT :tenant,'acc-1',CASE WHEN i<=:mapped THEN 'post-'||i ELSE 'history-'||i END,
          'ad-'||i,'VIDEO_SPARK_ADS','video-'||i,now()
        FROM generate_series(1,102000) i
        """),
        params,
    )
    connection.execute(
        text("""
        INSERT INTO material_report_day
          (tenant_id,advertiser_id,stat_date,main_material_id,main_material_type,
           currency,timezone,fetched_at,sync_job_id,spend,revenue_total)
        SELECT :tenant,'acc-1',CAST(:day AS date)-60,'aaa-history-'||i,'VIDEO_SPARK_ADS',
          'USD','UTC',now(),gen_random_uuid(),1,0
        FROM generate_series(1,100000) i
        """),
        params,
    )
    connection.execute(text("ANALYZE material_report_day"))
    connection.execute(text("ANALYZE material_post_link"))
    captured = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        captured.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert material_links.needs_material_links(
            session,
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            start=D - timedelta(days=9),
            end=D,
        ) is bool(missing_count)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(captured) == 1
    statement, parameters = captured[0]
    explanation = _explain_prepared(connection, statement, parameters, mode)
    record_property("actual_sql", statement)
    record_property("plan", json.dumps(explanation))
    mapping = [
        n
        for n in _nodes(explanation["Plan"])
        if n.get("Relation Name") == "material_post_link"
    ]
    assert explanation["Plan"]["Actual Rows"] == 1
    if missing_count == 2000:
        assert sum(n["Actual Loops"] for n in mapping) < 4000
    captured.clear()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert material_links.missing_video_ids(
            session,
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            start=D - timedelta(days=9),
            end=D,
        ) == {f"post-{i}" for i in range(4001 - missing_count, 4001)}
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(captured) == 1
    full_explanation = _explain_prepared(connection, *captured[0], mode)
    record_property("full_plan", json.dumps(full_explanation))
    if missing_count == 2000:
        # 除了返回一行，还要证实没有偷偷做完整 mapping 扫描/哈希构建。
        full_mapping = [
            n
            for n in _nodes(full_explanation["Plan"])
            if n.get("Relation Name") == "material_post_link"
        ]
        assert sum(
            n.get("Shared Hit Blocks", 0) + n.get("Shared Read Blocks", 0)
            for n in mapping
        ) < sum(
            n.get("Shared Hit Blocks", 0) + n.get("Shared Read Blocks", 0)
            for n in full_mapping
        )

    def fact_blocks(plan):
        return sum(
            n.get("Shared Hit Blocks", 0) + n.get("Shared Read Blocks", 0)
            for n in _nodes(plan)
            if n.get("Relation Name", "").startswith("material_report_day")
        )

    # 防止 bool 的低 startup 计划退化为扫窗口外历史的随机事实索引路径。
    # 比较同一数据/连接的物理工作量，不把 LIMIT 或本机毫秒数当成本证明。
    assert fact_blocks(explanation["Plan"]) <= 2 * fact_blocks(full_explanation["Plan"])


@pytest.mark.parametrize("kind", ["VIDEO_SPARK_ADS", "VIDEO_NON_SPARK_ADS"])
@pytest.mark.parametrize("video", [None, "", "valid-video"])
def test_existence_matches_full_set_and_preserves_scope(
    session, sync_seed, kind, video
):
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.modules.reporting.store_models import MaterialPostLink

    first = D - timedelta(days=1)
    for mid, before, after in [("paid", "1", "0"), ("cancelled", "1", "-1")]:
        for day, amount in [(first, before), (D, after)]:
            row = fact(session, sync_seed, mid, amount, "0", day=day)
            row.main_material_type = kind
            session.add(row)
    fact(session, sync_seed, "outside", "1", "0", day=D + timedelta(days=1))
    fact(session, sync_seed, "other-account", "1", "0", adv="acc-2")
    fact(session, sync_seed, "other-tenant", "1", "0", tenant=uuid4())
    for tenant, adv, vid in [
        (sync_seed.tenant_id, "acc-1", video),
        (sync_seed.tenant_id, "acc-2", "valid-video"),
        (uuid4(), "acc-1", "valid-video"),
    ]:
        session.add(
            MaterialPostLink(
                tenant_id=tenant,
                advertiser_id=adv,
                main_material_id="paid",
                ad_remote_id="ad",
                main_material_type=kind,
                platform_material_id=vid,
                observed_at=datetime.now(UTC),
            )
        )
    session.flush()
    args = {
        "tenant_id": sync_seed.tenant_id,
        "advertiser_id": "acc-1",
        "start": first,
        "end": D,
    }
    expected = {"paid"} if not video else set()
    assert material_links.missing_video_ids(session, **args) == expected
    assert material_links.needs_material_links(session, **args) is bool(expected)


def test_window_and_mapping_identity_remain_per_post_across_types(session, sync_seed):
    from datetime import UTC, datetime

    from app.modules.reporting.store_models import MaterialPostLink

    first = D - timedelta(days=1)
    fact(session, sync_seed, "post", "1", "0", day=first)
    correction = fact(session, sync_seed, "post", "-1", "0")
    correction.main_material_type = "VIDEO_NON_SPARK_ADS"
    session.flush()
    args = {
        "tenant_id": sync_seed.tenant_id,
        "advertiser_id": "acc-1",
        "start": first,
        "end": D,
    }
    # 类型变化不拆开同一帖子的净金额，也不阻止任一广告的真实 VID 证据。
    assert not material_links.needs_material_links(session, **args)
    assert material_links.missing_video_ids(session, **args) == set()
    correction.spend = 0
    session.flush()
    assert material_links.needs_material_links(session, **args)
    for ad, video, kind in [
        ("unknown", None, "VIDEO_SPARK_ADS"),
        ("empty", "", "VIDEO_SPARK_ADS"),
        ("mapped", "video", "VIDEO_NON_SPARK_ADS"),
    ]:
        session.add(
            MaterialPostLink(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id="post",
                ad_remote_id=ad,
                main_material_type=kind,
                platform_material_id=video,
                observed_at=datetime.now(UTC),
            )
        )
    session.flush()
    assert not material_links.needs_material_links(session, **args)
    assert material_links.missing_video_ids(session, **args) == set()
