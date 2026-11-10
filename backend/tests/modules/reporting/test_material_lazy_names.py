"""已知目录名不再展开历史帖子JSON；缺失名称仍按原期间口径回退。"""

from sqlalchemy import event, text

from tests.modules.reporting.test_material_query import (
    material_row,
    publish,
    query,
    use,
)


def test_page_only_reads_historical_names_for_missing_directory_names(
    session, sync_seed
):
    publish(
        session,
        sync_seed,
        "acc-1",
        [
            material_row("p1", "10", "不应使用的历史名"),
            material_row("p2", "20", "需要回退的历史名"),
        ],
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="v1",
        ad_material="m1",
        post="p1",
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a2",
        video="v2",
        ad_material="m2",
        post="p2",
    )
    session.execute(
        text(
            "UPDATE current_ad_material SET name='' WHERE tenant_id=:t AND platform_material_id='v2'"
        ),
        {"t": sync_seed.tenant_id},
    )
    requested = []
    connection = session.connection()

    def record(_conn, _cursor, statement, parameters, _context, _executemany):
        if "jsonb_each_text(d.post_names)" in statement and "row_keys" in parameters:
            requested.extend(parameters["row_keys"])

    event.listen(connection, "before_cursor_execute", record)
    try:
        rows = query(session, sync_seed, include_empty=False)
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert {r.display["main_material_id"]: r.display["name"] for r in rows} == {
        "v1": "v1.mp4",
        "v2": "需要回退的历史名",
    }
    assert requested == ['["material", "v2"]']
