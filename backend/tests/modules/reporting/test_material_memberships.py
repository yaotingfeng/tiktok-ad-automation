"""素材成员不可变版本：真实PG触发器、歧义与元数据更新合同。"""

from uuid import UUID

import pytest
from sqlalchemy import text

from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.read_models.material_memberships import material_statements


@pytest.fixture
def material_members(session, sync_seed):
    connection = session.connection()
    if not connection.execute(
        text("SELECT to_regclass('report_evidence_version')")
    ).scalar():
        connection.execute(
            text("""CREATE TABLE report_evidence_version (
            id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
            advertiser_id varchar(128) NOT NULL, kind varchar(32) NOT NULL,
            refs jsonb NOT NULL, material_uses jsonb NOT NULL, naming_versions jsonb NOT NULL,
            created_at timestamptz NOT NULL, retired_at timestamptz,
            UNIQUE(tenant_id,advertiser_id,id))""")
        )
    if not connection.execute(
        text("SELECT to_regclass('material_ad_member_current')")
    ).scalar():
        for statement in material_statements():
            connection.execute(text(statement))
    return session, sync_seed


def add(
    case,
    ad="ad-1",
    precise="m-1",
    name="video",
    version=1,
    complete=True,
    adv="acc-1",
    vid="vid",
    raw_id=None,
):
    session, seed = case
    fields = {
        "tenant_id": seed.tenant_id,
        "advertiser_id": adv,
        "ad_remote_id": ad,
        "platform_material_id": vid,
        "ad_material_id": precise,
        "material_type": "VIDEO",
        "name": name,
        "complete": complete,
        "published_version": version,
    }
    if raw_id:
        fields["id"] = raw_id
    obj = AdMaterialReference(**fields)
    session.add(obj)
    session.flush()
    return obj


def heads(case, table="material_account_member_current"):
    session, seed = case
    return (
        session.connection()
        .execute(
            text(
                f"SELECT * FROM {table} WHERE tenant_id=:t ORDER BY advertiser_id,platform_material_id"
            ),
            {"t": seed.tenant_id},
        )
        .mappings()
        .all()
    )


def evidence(case, version):
    return (
        case[0]
        .connection()
        .execute(
            text("SELECT * FROM report_evidence_version WHERE id=:v"), {"v": version}
        )
        .mappings()
        .one()
    )


def test_material_version_stability_metadata_and_identity(material_members):
    obj = add(material_members)
    old = heads(material_members)[0]["version_id"]
    single = heads(material_members, "material_ad_member_current")[0]["version_id"]
    obj.name, obj.published_version = "new name", 2
    material_members[0].flush()
    assert heads(material_members)[0]["version_id"] == old
    assert heads(material_members)[0]["name"] == "new name"
    assert (
        heads(material_members, "material_ad_member_current")[0]["version_id"] == single
    )
    obj.ad_material_id = "replacement"
    material_members[0].flush()
    new = heads(material_members)[0]["version_id"]
    assert new != old
    assert evidence(material_members, old)["retired_at"] is not None
    assert (
        evidence(material_members, old)["material_uses"][0]["ad_material_id"] == "m-1"
    )
    assert (
        evidence(material_members, new)["material_uses"][0]["ad_material_id"]
        == "replacement"
    )


def test_ties_keep_null_identity_and_first_raw_name(material_members):
    add(material_members, precise="a", name="z-name", raw_id=UUID(int=100))
    add(material_members, precise="b", name="a-name", raw_id=UUID(int=200))
    head = heads(material_members, "material_ad_member_current")[0]
    assert head["ad_material_id"] is None
    assert head["name"] == "z-name"
    assert head["raw_id"] == UUID(int=100)
    assert heads(material_members)[0]["use_count"] == 1
    assert (
        evidence(material_members, head["version_id"])["material_uses"][0][
            "ad_material_id"
        ]
        is None
    )


def test_complete_latest_and_precise_precedence(material_members):
    add(material_members, precise=None, version=9)
    precise = add(material_members, precise="precise", version=2)
    add(material_members, precise="incomplete", version=10, complete=False)
    assert (
        heads(material_members, "material_ad_member_current")[0]["ad_material_id"]
        == "precise"
    )
    material_members[0].delete(precise)
    material_members[0].flush()
    assert (
        heads(material_members, "material_ad_member_current")[0]["ad_material_id"]
        is None
    )


def test_multiple_accounts_ads_order_delete_and_rollback(material_members):
    add(material_members, ad="z", adv="acc-1")
    obj = add(material_members, ad="a", adv="acc-1", precise="m-2")
    add(material_members, ad="q", adv="acc-2")
    rows = heads(material_members)
    assert len(rows) == 2
    assert rows[0]["use_count"] == 2
    frozen = rows[0]["version_id"]
    payload = evidence(material_members, frozen)
    assert [r["remote_id"] for r in payload["refs"]] == ["a", "z"]
    assert [u["ad_ref"]["remote_id"] for u in payload["material_uses"]] == ["a", "z"]
    nested = material_members[0].begin_nested()
    material_members[0].delete(obj)
    material_members[0].flush()
    assert heads(material_members)[0]["use_count"] == 1
    nested.rollback()
    assert heads(material_members)[0]["version_id"] == frozen
    assert evidence(material_members, frozen)["retired_at"] is None


def test_last_use_deletion_retires_both_heads_and_moves_vid(material_members):
    obj = add(material_members)
    old_account = heads(material_members)[0]["version_id"]
    old_ad = heads(material_members, "material_ad_member_current")[0]["version_id"]
    obj.platform_material_id = "new-vid"
    material_members[0].flush()
    assert [h["platform_material_id"] for h in heads(material_members)] == ["new-vid"]
    assert evidence(material_members, old_ad)["retired_at"] is not None
    assert evidence(material_members, old_account)["retired_at"] is not None
    versions = [
        heads(material_members)[0]["version_id"],
        heads(material_members, "material_ad_member_current")[0]["version_id"],
    ]
    material_members[0].delete(obj)
    material_members[0].flush()
    assert heads(material_members) == []
    assert heads(material_members, "material_ad_member_current") == []
    assert all(
        evidence(material_members, version)["retired_at"] for version in versions
    )


def test_tenant_isolation_and_account_cascade_retires_orphan_heads(material_members):
    from types import SimpleNamespace
    from uuid import uuid4

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.tenants.models import Tenant

    session, seed = material_members
    tenant = Tenant(id=uuid4(), name="material-members-other")
    session.add(tenant)
    session.flush()
    account = AdvertiserAccount(
        tenant_id=tenant.id, advertiser_id="acc-1", timezone="UTC"
    )
    session.add(account)
    session.flush()
    other = (session, SimpleNamespace(tenant_id=tenant.id))
    add(material_members, precise="own")
    obj = add(other, precise="other")
    own_version = heads(material_members)[0]["version_id"]
    other_head = heads(other)[0]
    assert (
        evidence(other, other_head["version_id"])["material_uses"][0]["ad_material_id"]
        == "other"
    )
    assert (
        evidence(material_members, own_version)["material_uses"][0]["ad_material_id"]
        == "own"
    )
    session.delete(obj)
    session.flush()
    # 派生头可能由重建保留；账户删除必须能级联清理并退休，无需改canonical外键。
    session.connection().execute(
        text("""INSERT INTO material_account_member_current
        (tenant_id,advertiser_id,platform_material_id,version_id,use_count,name,ad_ids)
        VALUES(:tenant_id,:advertiser_id,:platform_material_id,:version_id,:use_count,:name,:ad_ids)"""),
        dict(other_head),
    )
    session.connection().execute(
        text("UPDATE report_evidence_version SET retired_at=NULL WHERE id=:v"),
        {"v": other_head["version_id"]},
    )
    session.delete(account)
    session.flush()
    assert heads(other) == []
    assert evidence(other, other_head["version_id"])["retired_at"] is not None
    assert heads(material_members)[0]["version_id"] == own_version


def test_empty_then_large_statement_and_noop_rebuild_preserve_versions(
    material_members,
):
    session, seed = material_members
    connection = session.connection()
    parameters = {"tenant": seed.tenant_id}
    # 首次空UPDATE不能形成污染后续批量的缓存计划；生产触发器按statement重新规划。
    for _ in range(6):
        connection.execute(
            text("UPDATE ad_material_reference SET name=name WHERE false")
        )
    connection.execute(
        text("""INSERT INTO ad_material_reference
        (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,material_type,
         name,creative_ids,complete,published_version)
        SELECT gen_random_uuid(),:tenant,'acc-1','batch-'||i,'shared-'||(i%10),'m-'||i,'VIDEO',
            'name-'||i,'[]'::jsonb,true,1 FROM generate_series(1,1000) i"""),
        parameters,
    )
    old_heads = heads(material_members)
    assert len(old_heads) == 10
    assert sum(h["use_count"] for h in old_heads) == 1000
    old_versions = {h["platform_material_id"]: h["version_id"] for h in old_heads}
    connection.execute(
        text("""UPDATE ad_material_reference SET published_version=2
        WHERE tenant_id=:tenant AND advertiser_id='acc-1'"""),
        parameters,
    )
    assert {
        h["platform_material_id"]: h["version_id"] for h in heads(material_members)
    } == old_versions
    connection.execute(
        text("""SELECT qrm_refresh_material_members(:tenant,'acc-1',platform_material_id)
        FROM (SELECT DISTINCT platform_material_id FROM current_ad_material
          WHERE tenant_id=:tenant AND advertiser_id='acc-1' AND material_type='VIDEO') v"""),
        parameters,
    )
    assert {
        h["platform_material_id"]: h["version_id"] for h in heads(material_members)
    } == old_versions
    total = connection.execute(
        text(
            "SELECT count(*) FROM report_evidence_version WHERE tenant_id=:tenant AND kind IN ('material_ad','material_account')"
        ),
        parameters,
    ).scalar_one()
    assert total == 1010
    plan = connection.execute(
        text("""EXPLAIN (FORMAT JSON) SELECT version_id FROM material_account_member_current
        WHERE tenant_id=:tenant AND platform_material_id='shared-1' AND advertiser_id='acc-1'"""),
        parameters,
    ).scalar_one()
    assert "Index" in str(plan)


def test_exact_post_identity_never_matches_ambiguous_head(material_members):
    session, seed = material_members
    add(material_members, precise="a")
    ambiguous = add(material_members, precise="b")
    session.connection().execute(
        text("""INSERT INTO material_post_link
        (tenant_id,advertiser_id,main_material_id,ad_remote_id,main_material_type,ad_material_id,observed_at)
        VALUES(:tenant,'acc-1','post-a','ad-1','VIDEO_SPARK_ADS','a',clock_timestamp())"""),
        {"tenant": seed.tenant_id},
    )
    statement = text("""SELECT h.version_id FROM material_ad_member_current h JOIN material_post_link p
        ON p.tenant_id=h.tenant_id AND p.advertiser_id=h.advertiser_id AND p.ad_remote_id=h.ad_remote_id
        AND p.ad_material_id=h.ad_material_id WHERE h.tenant_id=:tenant""")
    assert (
        session.connection().execute(statement, {"tenant": seed.tenant_id}).all() == []
    )
    session.delete(ambiguous)
    session.flush()
    assert (
        len(session.connection().execute(statement, {"tenant": seed.tenant_id}).all())
        == 1
    )


def test_rebuild_repairs_missing_derived_heads(material_members):
    session, seed = material_members
    add(material_members)
    old = heads(material_members)[0]["version_id"]
    session.connection().execute(
        text("DELETE FROM material_account_member_current WHERE tenant_id=:tenant"),
        {"tenant": seed.tenant_id},
    )
    session.connection().execute(
        text("SELECT qrm_refresh_material_members(:tenant,'acc-1','vid')"),
        {"tenant": seed.tenant_id},
    )
    assert heads(material_members)[0]["version_id"] != old
    assert heads(material_members)[0]["use_count"] == 1
    assert evidence(material_members, old)["retired_at"] is not None


def test_batch_same_video_refreshes_once_per_statement(material_members):
    session, seed = material_members
    connection = session.connection()
    connection.execute(text("SET LOCAL track_functions='pl'"))

    def refresh_count():
        return connection.execute(
            text("""SELECT COALESCE(sum(calls),0) FROM pg_stat_xact_user_functions
            WHERE funcname='qrm_refresh_material_members'""")
        ).scalar_one()

    before = refresh_count()
    connection.execute(
        text("""INSERT INTO ad_material_reference
        (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,material_type,
         name,creative_ids,complete,published_version)
        SELECT gen_random_uuid(),:tenant,'acc-1','batch-'||i,'one-vid','m-'||i,'VIDEO',
            'name-'||i,'[]'::jsonb,true,1 FROM generate_series(1,200) i"""),
        {"tenant": seed.tenant_id},
    )
    assert refresh_count() - before == 1
    before = refresh_count()
    connection.execute(
        text("DELETE FROM ad_material_reference WHERE tenant_id=:tenant"),
        {"tenant": seed.tenant_id},
    )
    assert refresh_count() - before == 1
