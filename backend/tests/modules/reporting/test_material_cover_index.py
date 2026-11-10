from sqlalchemy import text

from tests.modules.reporting.test_material_query import (
    D,
    material_row,
    publish,
)


def test_material_coverage_query_uses_covering_index_and_keeps_missing_accounts(
    session, sync_seed
):
    from app.modules.reporting.row_set import _common_ctes
    from tests.modules.reporting.query_helpers import finish_projections

    publish(session, sync_seed, "acc-1", [material_row("post", "10")])
    finish_projections(session)
    statement = "WITH " + _common_ctes("creative_material") + " SELECT * FROM cov"
    parameters = {
        "tenant": sync_seed.tenant_id,
        "advs": ["acc-1", "acc-2"],
        "start": D,
        "end": D,
        "ndays": 1,
    }
    assert dict(session.execute(text(statement), parameters).all()) == {
        "acc-1": True,
        "acc-2": False,
    }
    # 构造真实的跨期历史，避免单行表中两个等价索引的成本并列使计划断言随机。
    session.execute(
        text("""INSERT INTO read_bucket_state
        (tenant_id,advertiser_id,kind,bucket,source_revision,derived_revision,observed_at,derived_observed_at,derived_status)
        SELECT :tenant,'acc-1','creative_material',CAST(:start AS date)-n,1,1,now(),now(),'COMPLETE'
        FROM generate_series(1,1000) n ON CONFLICT DO NOTHING"""),
        parameters,
    )
    session.execute(text("ANALYZE read_bucket_state"))
    session.execute(text("SET LOCAL enable_seqscan=off"))
    session.execute(text("SET LOCAL enable_bitmapscan=off"))
    plan = session.execute(
        text("EXPLAIN (ANALYZE,FORMAT JSON) " + statement), parameters
    ).scalar_one()[0]["Plan"]

    def scans(node):
        return (
            [node]
            if node.get("Relation Name") == "read_bucket_state"
            else [scan for child in node.get("Plans", []) for scan in scans(child)]
        )

    state_scans = scans(plan)
    assert state_scans
    assert all(scan["Node Type"] == "Index Only Scan" for scan in state_scans), (
        state_scans
    )
    assert all(
        scan["Index Name"] == "ix_read_bucket_projection_period" for scan in state_scans
    )
