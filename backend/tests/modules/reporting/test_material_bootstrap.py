"""冻结源的素材bootstrap按完整VID批量写入，不调用运行时锁刷新。"""

from uuid import UUID

import pytest
from sqlalchemy import text

from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.read_models.material_memberships import (
    material_bootstrap_statements,
    material_statements,
)


def _read(session, statement, **params):
    return session.connection().execute(text(statement), params)


def _payload(session):
    ads = _read(
        session,
        """SELECT h.tenant_id,h.advertiser_id,h.ad_remote_id,h.platform_material_id,
        h.material_type,h.ad_material_id,h.name,h.raw_id,h.published_version,v.refs,v.material_uses
        FROM material_ad_member_current h JOIN report_evidence_version v ON v.id=h.version_id
        ORDER BY h.tenant_id,h.advertiser_id,h.ad_remote_id,h.platform_material_id""",
    ).all()
    accounts = _read(
        session,
        """SELECT h.tenant_id,h.advertiser_id,h.platform_material_id,h.use_count,h.name,h.ad_ids,
        v.refs,v.material_uses FROM material_account_member_current h
        JOIN report_evidence_version v ON v.id=h.version_id
        ORDER BY h.tenant_id,h.advertiser_id,h.platform_material_id""",
    ).all()
    return ads, accounts


@pytest.fixture
def bootstrap_source(session, sync_seed):
    # canonical完整/最新投影继续运行；仅暂停本轮待bootstrap的下游成员触发器。
    _read(
        session,
        "ALTER TABLE ad_material_reference DISABLE TRIGGER zz_material_members_insert",
    )
    cases = [
        (100, "acc-1", "a", "v1", "b", "z-name", 3, True, "VIDEO"),
        (200, "acc-1", "a", "v1", "a", "a-name", 3, True, "VIDEO"),
        (300, "acc-1", "b", "v1", None, "", 1, True, "VIDEO"),
        (400, "acc-1", "c", "v2", "precise", "v2-name", 2, True, "VIDEO"),
        (500, "acc-1", "c", "v2", "incomplete", "wrong", 9, False, "VIDEO"),
        (600, "acc-1", "c", "v2", None, "wrong", 10, True, "VIDEO"),
        (700, "acc-1", "d", "v3", "last", "v3-name", 1, True, "VIDEO"),
        (800, "acc-2", "a", "v1", None, "other-account", 1, True, "VIDEO"),
        (900, "acc-1", "image", "image", None, "image", 1, True, "IMAGE"),
    ]
    for ident, adv, ad, vid, precise, name, version, complete, kind in cases:
        session.add(
            AdMaterialReference(
                id=UUID(int=ident),
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                ad_remote_id=ad,
                platform_material_id=vid,
                ad_material_id=precise,
                material_type=kind,
                name=name,
                published_version=version,
                complete=complete,
            )
        )
    session.flush()
    _read(
        session,
        "ALTER TABLE ad_material_reference ENABLE TRIGGER zz_material_members_insert",
    )
    return session, sync_seed


def test_bootstrap_matches_runtime_payload_without_advisory_or_runtime_calls(
    bootstrap_source,
):
    session, _ = bootstrap_source
    _read(session, "SET LOCAL track_functions='pl'")
    locks = _read(
        session,
        "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'",
    ).scalar_one()
    calls = _read(
        session,
        "SELECT COALESCE(sum(calls),0) FROM pg_stat_xact_user_functions WHERE funcname='qrm_refresh_material_members'",
    ).scalar_one()
    nested = session.begin_nested()
    for statement in material_bootstrap_statements(batch_rows=2):
        _read(session, statement)
    actual = _payload(session)
    assert len(actual[0]) == 5
    assert len(actual[1]) == 4
    assert actual[0][0].ad_material_id is None
    assert actual[0][0].name == "z-name"
    assert actual[0][0].raw_id == UUID(int=100)
    assert actual[0][2].ad_material_id == "precise"
    assert (
        _read(
            session,
            "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'",
        ).scalar_one()
        == locks
    )
    assert (
        _read(
            session,
            "SELECT COALESCE(sum(calls),0) FROM pg_stat_xact_user_functions WHERE funcname='qrm_refresh_material_members'",
        ).scalar_one()
        == calls
    )
    nested.rollback()
    for tenant, adv, vid in _read(
        session,
        "SELECT DISTINCT tenant_id,advertiser_id,platform_material_id FROM current_ad_material WHERE material_type='VIDEO' ORDER BY 1,2,3",
    ):
        _read(
            session,
            "SELECT qrm_refresh_material_members(:tenant,:adv,:vid)",
            tenant=tenant,
            adv=adv,
            vid=vid,
        )
    assert _payload(session) == actual


def test_bootstrap_empty_source_and_final_foreign_keys(session):
    for statement in material_bootstrap_statements(batch_rows=2):
        _read(session, statement)
    assert _payload(session) == ([], [])
    statements = material_statements()
    bootstrap_pos = next(
        i
        for i, statement in enumerate(statements)
        if "DO $material_bootstrap$" in statement
    )
    foreign_keys = [
        i
        for i, statement in enumerate(statements)
        if "ADD CONSTRAINT fk_material_" in statement
    ]
    assert len(foreign_keys) == 4
    assert min(foreign_keys) > bootstrap_pos
    assert sum("VALIDATE CONSTRAINT fk_material_" in s for s in statements) == 4


@pytest.mark.parametrize("batch_rows", [0, -1, True])
def test_bootstrap_rejects_invalid_batch_size(batch_rows):
    with pytest.raises(ValueError):
        material_bootstrap_statements(batch_rows=batch_rows)


def test_full_material_template_backfills_then_validates_all_foreign_keys(
    bootstrap_source,
):
    session, _ = bootstrap_source
    # 独立测试事务中重装本轮派生组件；canonical/current事实与通用证据表保持原样。
    for operation in ("insert", "update", "delete"):
        _read(
            session,
            f"DROP TRIGGER zz_material_members_{operation} ON ad_material_reference",
        )
        _read(session, f"DROP FUNCTION qrm_material_members_{operation}()")
    _read(
        session, "DROP TRIGGER zz_material_members_account_insert ON advertiser_account"
    )
    _read(session, "DROP FUNCTION qrm_material_members_account_insert()")
    _read(session, "DROP FUNCTION qrm_refresh_material_members(uuid,text,text)")
    _read(
        session, "DROP TABLE material_ad_member_current,material_account_member_current"
    )
    _read(session, "DROP FUNCTION qrm_retire_material_member()")
    for statement in material_statements():
        _read(session, statement)
    ads, accounts = _payload(session)
    assert len(ads) == 5
    assert len(accounts) == 4
    constraints = _read(
        session,
        """SELECT conname,convalidated FROM pg_constraint
        WHERE conrelid IN ('material_ad_member_current'::regclass,'material_account_member_current'::regclass)
        AND contype='f'""",
    ).all()
    assert len(constraints) == 4
    assert all(constraint.convalidated for constraint in constraints)


def test_bootstrap_batches_are_bounded_without_splitting_video_groups(
    session, sync_seed
):
    _read(
        session,
        "ALTER TABLE ad_material_reference DISABLE TRIGGER zz_material_members_insert",
    )
    _read(
        session,
        """INSERT INTO ad_material_reference
        (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,material_type,name,complete,published_version)
        SELECT gen_random_uuid(),:tenant,'acc-1','batch-'||i,'video-'||((i-1)/3),'m-'||i,'VIDEO','name',true,1
        FROM generate_series(1,10) i""",
        tenant=sync_seed.tenant_id,
    )
    _read(
        session,
        "ALTER TABLE ad_material_reference ENABLE TRIGGER zz_material_members_insert",
    )
    _read(
        session,
        "CREATE TEMP TABLE bootstrap_batch_audit(batch_id serial,rows bigint,vids text[]) ON COMMIT DROP",
    )
    _read(
        session,
        """CREATE FUNCTION pg_temp.audit_bootstrap_batch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN INSERT INTO bootstrap_batch_audit(rows,vids)
            SELECT count(*),array_agg(DISTINCT platform_material_id) FROM written_rows;
            RETURN NULL; END $$""",
    )
    _read(
        session,
        """CREATE TRIGGER audit_bootstrap_batch AFTER INSERT ON material_ad_member_current
        REFERENCING NEW TABLE AS written_rows FOR EACH STATEMENT EXECUTE FUNCTION pg_temp.audit_bootstrap_batch()""",
    )
    for statement in material_bootstrap_statements(batch_rows=4):
        _read(session, statement)
    sizes = (
        _read(session, "SELECT rows FROM bootstrap_batch_audit ORDER BY batch_id")
        .scalars()
        .all()
    )
    assert sizes == [6, 3, 1]
    assert max(sizes) <= 4 + 3  # 批次预算加一个不可拆分最大VID组的严格上界。
    assert (
        _read(
            session,
            """SELECT max(n) FROM (SELECT vid,count(*) n FROM bootstrap_batch_audit
        CROSS JOIN LATERAL unnest(vids) v(vid) GROUP BY vid) appearances""",
        ).scalar_one()
        == 1
    )
    assert _read(
        session,
        "SELECT array_agg(use_count ORDER BY platform_material_id) FROM material_account_member_current",
    ).scalar_one() == [3, 3, 3, 1]
