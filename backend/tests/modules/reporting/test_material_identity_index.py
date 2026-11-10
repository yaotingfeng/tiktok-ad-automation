"""聚合身份的排序规则不能使后续账户索引失效。"""

from sqlalchemy import text

from app.modules.reporting.creative_query import filtered_ctes
from app.modules.reporting.row_set import _Params
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.query_helpers import finish_projections
from tests.modules.reporting.test_material_query import D, material_row, publish, use


def test_material_fact_account_keeps_storage_collation(session, sync_seed):
    publish(session, sync_seed, "acc-1", [material_row("post", "10")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad",
        video="vid",
        ad_material="m",
        post="post",
    )
    finish_projections(session)
    facts = filtered_ctes(
        ReportingFilter(dimension="material", start_date=D, end_date=D), _Params()
    )
    params = {"tenant": sync_seed.tenant_id, "advs": ["acc-1"], "start": D, "end": D}
    actual, stored = session.execute(
        text(
            "WITH "
            + facts
            + " SELECT pg_collation_for(f.advertiser_id), pg_collation_for(l.advertiser_id) "
            "FROM creative_filtered f JOIN ad_creative l ON l.tenant_id=:tenant AND l.advertiser_id=f.advertiser_id"
        ),
        params,
    ).one()
    assert actual == stored
    # 即使本地默认locale恰好为C，显式C与索引的default仍是不同collation OID。
    session.execute(text("SET LOCAL enable_seqscan=off"))
    plan = session.execute(
        text(
            "EXPLAIN (FORMAT JSON) WITH " + facts + " SELECT l.ad_remote_id "
            "FROM creative_filtered f CROSS JOIN LATERAL (SELECT * FROM ad_creative l "
            "WHERE l.tenant_id=:tenant AND l.advertiser_id=f.advertiser_id OFFSET 0) l"
        ),
        params,
    ).scalar_one()[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    probes = [n for n in nodes(plan) if n.get("Relation Name") == "ad_creative"]
    assert probes and all("advertiser_id" in n.get("Index Cond", "") for n in probes)
