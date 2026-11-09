"""真实 PG 验证 Spark 先筛缺桥接候选，再按完整窗口聚合金额。"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from app.modules.reporting.partitions import ensure_month_partitions_for_range
from app.modules.reporting.spark_identity import pending_spark_links
from app.modules.reporting.store_models import MaterialPostLink, MaterialReportDay
from tests.modules.reporting.test_material_link_existence import _explain_prepared
from tests.modules.reporting.test_material_query import D


def job(seed, *, start=D, end=D):
    return SimpleNamespace(
        tenant_id=seed.tenant_id,
        advertiser_ids=["acc-1"],
        start_date=start,
        end_date=end,
        progress={},
    )


def fact(
    seed,
    mid,
    *,
    spend="1",
    revenue="0",
    day=D,
    tenant=None,
    advertiser="acc-1",
    kind="VIDEO_SPARK_ADS",
):
    return MaterialReportDay(
        tenant_id=tenant or seed.tenant_id,
        advertiser_id=advertiser,
        stat_date=day,
        main_material_id=mid,
        main_material_type=kind,
        currency="USD",
        timezone="UTC",
        fetched_at=datetime.now(UTC),
        sync_job_id=uuid4(),
        spend=Decimal(spend) if spend is not None else None,
        revenue_total=Decimal(revenue) if revenue is not None else None,
    )


def link(seed, mid, *, owner=None, **kwargs):
    return MaterialPostLink(
        tenant_id=seed.tenant_id,
        advertiser_id="acc-1",
        main_material_id=mid,
        main_material_type="VIDEO_SPARK_ADS",
        ad_remote_id=owner or f"ad-{mid}",
        observed_at=datetime.now(UTC),
        **kwargs,
    )


def pending_with_plan(session, item, *, mode=None):
    captured = []
    connection = session.connection()

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        captured.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        rows = pending_spark_links(session, item)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(captured) == 1
    statement, parameters = captured[0]
    if mode is not None:
        plan = _explain_prepared(connection, statement, parameters, mode)["Plan"]
    else:
        plan = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
        ).scalar_one()[0]["Plan"]
    return rows, plan


def fact_scans(plan):
    found = []
    if plan.get("Relation Name", "").startswith("material_report_day"):
        found.append(plan)
    for child in plan.get("Plans", []):
        found.extend(fact_scans(child))
    return found


def test_frozen_dates_render_safely_without_inlining_scope(session, sync_seed):
    first = D - timedelta(days=1)
    item = job(sync_seed, start=first)
    checked = "unmatched'); SELECT 'still bound"
    item.progress = {"spark_identity_checked": [checked]}
    session.add_all([fact(sync_seed, "post"), link(sync_seed, "post")])
    session.flush()
    captured = []
    connection = session.connection()

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        captured.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        rows = pending_spark_links(session, item)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert [row.main_material_id for row in rows] == ["post"]
    assert len(captured) == 1
    statement, parameters = captured[0]
    assert f"'{first.isoformat()}'" in statement
    assert f"'{D.isoformat()}'" in statement
    assert "value_scan_start" not in parameters
    assert "value_scan_end" not in parameters
    assert parameters["t"] == parameters["value_tenant"] == sync_seed.tenant_id
    assert parameters["a"] == "acc-1"
    assert parameters["value_advertisers"] == ["acc-1"]
    assert parameters["value_starts"] == [first]
    assert parameters["value_ends"] == [D]
    assert parameters["checked"] == [checked]
    assert str(sync_seed.tenant_id) not in statement
    assert "acc-1" not in statement
    assert checked not in statement


def test_orphan_link_without_window_facts_is_not_pending(session, sync_seed):
    session.add(link(sync_seed, "orphan"))
    # 相同素材号的其他作用域和窗口外事实不能为孤立关联提供金额证据。
    session.add_all(
        [
            fact(sync_seed, "orphan", advertiser="acc-2"),
            fact(sync_seed, "orphan", tenant=uuid4()),
            fact(sync_seed, "orphan", kind="VIDEO_NON_SPARK_ADS"),
            fact(sync_seed, "orphan", day=D - timedelta(days=1)),
        ]
    )
    session.flush()
    assert pending_spark_links(session, job(sync_seed)) == []


@pytest.mark.parametrize("candidates", [0, 2, 134])
@pytest.mark.parametrize("mode", ["force_custom_plan", "force_generic_plan"])
def test_fact_reads_follow_missing_candidates_not_all_account_materials(
    session, sync_seed, candidates, mode, record_property
):
    # 独立月份分区随本测试事务回滚，避免其他大批量回滚夹具遗留的
    # 索引物理体积误导优化器；结构/索引仍由真实生产分区入口生成。
    plan_day = date(2040, 1, 5)
    ensure_month_partitions_for_range(
        session, plan_day, plan_day, tables=("material_report_day",)
    )
    # 已桥接的账户事实仍存在，但不能在每个小步扫描它们的金额。
    for index in range(600):
        mid = f"material-{index:04d}"
        session.add(fact(sync_seed, mid, day=plan_day))
        session.add(link(sync_seed, mid, platform_material_id="resolved"))
    for index in range(candidates):
        mid = f"candidate-{index:04d}"
        session.add(fact(sync_seed, mid, day=plan_day))
        session.add(link(sync_seed, mid))
    session.flush()
    # 批量事实写入后更新真实统计，避免测试事务内尚未 ANALYZE 的空表
    # 估算把相关索引读取误选成按账户反复扫描。
    session.connection().execute(text("ANALYZE material_report_day"))
    session.connection().execute(text("ANALYZE material_post_link"))
    rows, plan = pending_with_plan(
        session, job(sync_seed, start=plan_day, end=plan_day), mode=mode
    )
    record_property("plan", json.dumps(plan))
    assert len(rows) == min(candidates, 50)
    scans = fact_scans(plan)
    if not candidates:
        assert all(node["Actual Loops"] == 0 for node in scans)
    else:
        visited = sum(
            (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
            * node["Actual Loops"]
            for node in scans
        )
        # 每素材只聚合一次；留余量给分区/计划差异，
        # 同时拒绝全账户扫描和 material_values 内联导致的平方级重复。
        assert visited <= candidates * 4, scans


@pytest.mark.parametrize(
    "first_spend,last_spend,first_revenue,last_revenue,expected",
    [
        ("1", "-1", "0", "0", False),
        ("1", "-0.99", "0", "0", True),
        ("-2", "1", "0", "0", False),
        ("0", "0", "1", "-1", False),
        (None, None, "0", "0.01", True),
    ],
)
def test_candidate_value_preserves_net_window_and_scope(
    session, sync_seed, first_spend, last_spend, first_revenue, last_revenue, expected
):
    first = D - timedelta(days=1)
    session.add_all(
        [
            fact(
                sync_seed, "post", spend=first_spend, revenue=first_revenue, day=first
            ),
            fact(sync_seed, "post", spend=last_spend, revenue=last_revenue),
            fact(sync_seed, "post", spend="999", advertiser="acc-2"),
            fact(sync_seed, "post", spend="999", tenant=uuid4()),
            fact(sync_seed, "post", spend="999", day=first - timedelta(days=1)),
            fact(sync_seed, "post", spend="999", day=D + timedelta(days=1)),
            link(sync_seed, "post", owner="owner-a"),
            link(
                sync_seed,
                "post",
                owner="owner-b",
                ad_material_id="",
                platform_material_id="",
            ),
        ]
    )
    session.flush()
    rows = pending_spark_links(session, job(sync_seed, start=first))
    assert [(r.main_material_id, r.ad_remote_id) for r in rows] == (
        [("post", "owner-a"), ("post", "owner-b")] if expected else []
    )


def test_many_owners_aggregate_shared_material_only_once(session, sync_seed):
    session.add(fact(sync_seed, "shared"))
    session.add_all(
        [link(sync_seed, "shared", owner=f"owner-{index:02d}") for index in range(40)]
    )
    session.flush()
    rows, plan = pending_with_plan(session, job(sync_seed))
    assert len(rows) == 40
    value_reads = sum(
        node["Actual Rows"] * node["Actual Loops"]
        for node in fact_scans(plan)
        if node.get("Alias", "").split("_")[0] == "f"
    )
    assert value_reads == 1


@pytest.mark.parametrize(
    "spark_amount,other_amount,expected", [("0", "9", False), ("1", "-9", True)]
)
def test_same_id_other_material_type_cannot_lend_or_cancel_value(
    session, sync_seed, spark_amount, other_amount, expected
):
    first = D - timedelta(days=1)
    session.add_all(
        [
            fact(sync_seed, "post", spend=spark_amount),
            fact(
                sync_seed,
                "post",
                spend=other_amount,
                day=first,
                kind="VIDEO_NON_SPARK_ADS",
            ),
            link(sync_seed, "post"),
        ]
    )
    session.flush()
    assert bool(pending_spark_links(session, job(sync_seed, start=first))) is expected
