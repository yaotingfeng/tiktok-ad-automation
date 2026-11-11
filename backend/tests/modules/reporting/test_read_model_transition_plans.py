"""同连接不同transition规模必须重规划，旧新身份失效仍与事实写入原子提交。"""

import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


@contextmanager
def _plans(session):
    connection = session.connection()
    raw = connection.connection.driver_connection
    plans = []

    def notice(message):
        value = message.message_primary
        if "plan:\n" not in value or not any(
            marker in value for marker in ("current_ad_material",)
        ):
            return
        parsed = json.loads(value.split("plan:\n", 1)[1])
        if any(
            marker in parsed.get("Query Text", "")
            for marker in ("FROM new_rows", "FROM old_rows")
        ):
            plans.append(parsed)

    raw.add_notice_handler(notice)
    try:
        for setting in (
            "LOAD 'auto_explain'",
            "SET LOCAL auto_explain.log_min_duration='0ms'",
            "SET LOCAL auto_explain.log_nested_statements=on",
            "SET LOCAL auto_explain.log_analyze=on",
            "SET LOCAL auto_explain.log_buffers=on",
            "SET LOCAL auto_explain.log_timing=off",
            "SET LOCAL auto_explain.log_format=json",
            "SET LOCAL client_min_messages=log",
        ):
            connection.execute(text(setting))
        yield plans
    finally:
        raw.remove_notice_handler(notice)
        connection.execute(text("SET LOCAL auto_explain.log_min_duration=-1"))


def _seed(session, seed, _family, tag, n):
    statement = """INSERT INTO ad_material_reference
        (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,material_type,name,complete,published_version)
        SELECT gen_random_uuid(),:tenant,'acc-1',:tag||i,'video-'||i,i::text,'VIDEO','material-'||i,true,1
        FROM generate_series(1,:n)i"""
    session.execute(text(statement), {"tenant": seed.tenant_id, "tag": tag, "n": n})


@pytest.mark.parametrize("family", ["reference"])
@pytest.mark.parametrize("operation", ["INSERT", "UPDATE", "DELETE"])
def test_transition_estimate_tracks_small_large_small_on_one_backend(
    session, sync_seed, family, operation, record_property
):
    table = "ad_material_reference"
    counts = []
    for phase, n in enumerate([1] * 6 + [1000, 1]):
        tag = f"phase-{phase}-"
        if operation != "INSERT":
            _seed(session, sync_seed, family, tag, n)
        with _plans(session) as plans:
            if operation == "INSERT":
                _seed(session, sync_seed, family, tag, n)
            else:
                assignment = "name='updated'"
                prefix = (
                    f"UPDATE {table} SET {assignment}"
                    if operation == "UPDATE"
                    else f"DELETE FROM {table}"
                )
                session.execute(
                    text(
                        prefix + " WHERE tenant_id=:tenant AND ad_remote_id LIKE :tag"
                    ),
                    {"tenant": sync_seed.tenant_id, "tag": tag + "%"},
                )
        transitions = [
            node
            for p in plans
            for node in _nodes(p["Plan"])
            if node["Node Type"] == "Named Tuplestore Scan" and node["Actual Loops"] > 0
        ]
        assert transitions, plans
        counts.append([(x["Plan Rows"], x["Actual Rows"]) for x in transitions])
        # 固定SQL仍须使用本次transition实际基数，不能以LIMIT/总耗时掩盖缓存1行。
        assert all(
            x["Actual Rows"] == n and x["Plan Rows"] == n for x in transitions
        ), counts
    record_property("transition_estimates", json.dumps(counts))


def _buckets(session, tenant):
    return {
        (r.advertiser_id, r.kind, r.bucket): (r.source_revision, r.derived_revision)
        for r in session.execute(
            text("SELECT * FROM read_bucket_state WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )
    }


def test_reference_identity_move_keeps_precise_latest_ties_and_atomic_rollback(
    session, sync_seed
):
    ids = {
        key: uuid4() for key in ("generic", "a", "b", "old", "incomplete", "neighbor")
    }
    for key, material, version, complete, account in [
        ("generic", None, 9, True, "acc-1"),
        ("a", "a", 2, True, "acc-1"),
        ("b", "b", 2, True, "acc-1"),
        ("old", "old", 1, True, "acc-1"),
        ("incomplete", "incomplete", 20, False, "acc-1"),
        ("neighbor", "neighbor", 1, True, "acc-2"),
    ]:
        session.execute(
            text("""INSERT INTO ad_material_reference(id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,material_type,name,complete,published_version)
            VALUES(:id,:tenant,:account,'ad','video',:material,'VIDEO','name',:complete,:version)"""),
            {
                "id": ids[key],
                "tenant": sync_seed.tenant_id,
                "account": account,
                "material": material,
                "complete": complete,
                "version": version,
            },
        )

    def projected():
        return set(
            session.execute(
                text("SELECT id FROM current_ad_material WHERE tenant_id=:tenant"),
                {"tenant": sync_seed.tenant_id},
            ).scalars()
        )

    assert projected() == {ids["a"], ids["b"], ids["neighbor"]}
    initial = _buckets(session, sync_seed.tenant_id)
    sp = session.begin_nested()
    session.execute(
        text(
            "UPDATE ad_material_reference SET advertiser_id='acc-2',ad_remote_id='moved',platform_material_id='new-video' WHERE id IN (:a,:b)"
        ),
        {"a": ids["a"], "b": ids["b"]},
    )
    assert projected() == {ids["old"], ids["a"], ids["b"], ids["neighbor"]}
    moved = _buckets(session, sync_seed.tenant_id)
    for account in ("acc-1", "acc-2"):
        key = next(k for k in initial if k[0] == account and k[1] == "usage")
        assert moved[key][0] == initial[key][0] + 1 and moved[key][0] == moved[key][1]
    session.execute(
        text("DELETE FROM ad_material_reference WHERE id IN (:a,:b)"),
        {"a": ids["a"], "b": ids["b"]},
    )
    assert projected() == {ids["old"], ids["neighbor"]}
    sp.rollback()
    assert projected() == {ids["a"], ids["b"], ids["neighbor"]}
    assert _buckets(session, sync_seed.tenant_id) == initial


def test_fixed_migration_roundtrip_matches_generated_functions(session):
    import runpy
    from pathlib import Path

    from app.modules.reporting.read_models.ddl import trigger_statements

    migration = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "app/alembic/versions/0099_transition_replan.py"
        )
    )
    expected = [
        s.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
        for s in trigger_statements()
        if s.startswith(
            (
                "CREATE FUNCTION qrm_material_post_link_",
                "CREATE FUNCTION qrm_ad_material_reference_",
            )
        )
    ]
    # 历史0099同时包含现已删除的补链函数，当前只比较仍存续的素材引用函数。
    migration = {
        key: tuple(sql for sql in migration[key] if "qrm_ad_material_reference_" in sql)
        for key in ("UPGRADE", "DOWNGRADE")
    }
    assert migration["UPGRADE"] == tuple(expected)
    names = [s.split("()", 1)[0].split()[-1] for s in migration["UPGRADE"]]

    def definitions():
        return {
            name: session.execute(
                text("SELECT pg_get_functiondef(CAST(:function AS regprocedure))"),
                {"function": name + "()"},
            ).scalar_one()
            for name in names
        }

    before = definitions()
    savepoint = session.begin_nested()
    for statement in migration["DOWNGRADE"]:
        session.connection().exec_driver_sql(statement)
    assert all("$qrm_transition$" not in value for value in definitions().values())
    for statement in migration["UPGRADE"]:
        session.connection().exec_driver_sql(statement)
    assert definitions() == before
    savepoint.rollback()
    assert definitions() == before
