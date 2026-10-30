"""窗口粗界只用于裁剪；精确窗口的净额与开放边界仍由原关系决定。"""

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import event, text

from app.modules.reporting import material_links, material_value
from tests.modules.reporting.test_material_link_existence import (
    _explain_prepared,
    _nodes,
)
from tests.modules.reporting.test_material_query import D
from tests.modules.reporting.test_material_value import fact


@pytest.mark.parametrize("mode", ["force_custom_plan", "force_generic_plan"])
@pytest.mark.parametrize("exists", [False, True])
def test_window_prunes_other_months_and_same_month_history(
    session, sync_seed, mode, exists, record_property
):
    connection = session.connection()
    connection.execute(
        text("""
        INSERT INTO material_report_day
          (tenant_id,advertiser_id,stat_date,main_material_id,main_material_type,
           currency,timezone,fetched_at,sync_job_id,spend,revenue_total)
        SELECT :tenant,'acc-1',d,'history-'||i,'VIDEO_SPARK_ADS',
          'USD','UTC',now(),gen_random_uuid(),1,0
        FROM generate_series(1,20000) i CROSS JOIN
          unnest(ARRAY[date '2026-08-10',date '2026-09-10',
                       date '2026-10-20',date '2026-11-10']) d
        """),
        {"tenant": sync_seed.tenant_id},
    )
    for i in range(10):
        fact(session, sync_seed, f"paid-{i}", "1", "0")
    connection.execute(text("ANALYZE material_report_day"))
    captured = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        captured.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        if exists:
            assert material_links.needs_material_links(
                session,
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                start=D,
                end=D,
            )
        else:
            assert material_value.material_window_values(
                session, sync_seed.tenant_id, [("acc-1", D, D)]
            ) == {("acc-1", f"paid-{i}"): True for i in range(10)}
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(captured) == 1
    explanation = _explain_prepared(connection, *captured[0], mode)
    record_property("actual_sql", captured[0][0])
    record_property("plan", json.dumps(explanation))
    scans = [
        node
        for node in _nodes(explanation["Plan"])
        if node.get("Relation Name", "").startswith("material_report_day")
        and node.get("Actual Loops", 0) > 0
    ]
    # generic 必须实际裁剪，而不是只在 custom 常量计划里消去其他月份。
    assert {node["Relation Name"] for node in scans} == {"material_report_day_2026_10"}
    assert (
        sum(
            (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
            * node["Actual Loops"]
            for node in scans
        )
        <= 20
    )
    assert (
        sum(
            node.get("Shared Hit Blocks", 0) + node.get("Shared Read Blocks", 0)
            for node in scans
        )
        < 64
    )


@pytest.mark.parametrize(
    "windows,expected",
    [
        ([], {}),
        ([("acc-1", None, None)], {("acc-1", "post"): False}),
        ([("acc-1", None, date(2026, 9, 30))], {("acc-1", "post"): True}),
        ([("acc-1", date(2026, 10, 1), None)], {("acc-1", "post"): False}),
        ([("acc-1", date(2026, 9, 30), date(2026, 10, 1))], {("acc-1", "post"): False}),
        (
            [
                ("acc-1", date(2026, 9, 30), date(2026, 10, 1)),
                ("acc-1", date(2026, 9, 30), date(2026, 9, 30)),
            ],
            {("acc-1", "post"): True},
        ),
        (
            [("acc-1", None, date(2026, 9, 30)), ("acc-1", date(2026, 10, 1), None)],
            {("acc-1", "post"): True},
        ),
        ([("acc-1", date(2026, 10, 1), date(2026, 9, 30))], {}),
        (
            [
                ("acc-1", date(2026, 9, 29), date(2026, 9, 29)),
                ("acc-1", date(2026, 10, 2), date(2026, 10, 2)),
            ],
            {},
        ),
        (
            [
                ("acc-1", date(2026, 9, 30), date(2026, 9, 30)),
                ("acc-2", date(2026, 10, 1), date(2026, 10, 1)),
            ],
            {("acc-1", "post"): True, ("acc-2", "post"): True},
        ),
    ],
)
def test_coarse_bounds_keep_exact_window_net_values(
    session, sync_seed, windows, expected
):
    first = date(2026, 9, 30)
    fact(session, sync_seed, "post", "1", "0", day=first)
    correction = fact(
        session, sync_seed, "post", "-1", "0", day=first + timedelta(days=1)
    )
    correction.main_material_type = "VIDEO_NON_SPARK_ADS"
    fact(
        session, sync_seed, "post", "5", "0", day=first + timedelta(days=1), adv="acc-2"
    )
    session.flush()
    assert (
        material_value.material_window_values(session, sync_seed.tenant_id, windows)
        == expected
    )
