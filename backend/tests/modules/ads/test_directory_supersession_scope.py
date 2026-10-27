"""更新目录必须覆盖原请求语义，局部在投刷新不能淘汰含删除对象的依赖。"""

from uuid import uuid4

import pytest

from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
from app.modules.ads.sync import (
    directory_run_superseded,
    publish_directory,
    stage_directory_page,
)
from tests.modules.ads.test_directory_sync import _authority, _run


def _query(run, *, ids=(), parents=(), deleted=False):
    run.query = run.query | {
        "ids": list(ids),
        "parent_ids": list(parents),
        "include_deleted": deleted,
    }


def test_live_partial_publication_cannot_discard_background_deleted_dependency(
    session, directory_seed
):
    _authority(session, directory_seed)
    old = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    old.workload = "background"
    _query(old, ids=["deleted-ad"], deleted=True)
    new = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    _query(new, ids=["live-ad"])
    session.flush()
    page = DirectoryPage(
        items=(),
        materials=(),
        page=1,
        next_page=None,
        complete=True,
        evidence=CallEvidence(request_id="scope-regression"),
    )
    stage_directory_page(session, run_id=new.id, claim_generation=1, page=page)
    publish_directory(session, run_id=new.id, claim_generation=1)
    assert not directory_run_superseded(session, old)
    stage_directory_page(session, run_id=old.id, claim_generation=1, page=page)
    assert (
        publish_directory(session, run_id=old.id, claim_generation=1)
        > new.published_version
    )


@pytest.mark.parametrize(
    "old_ids,old_parents,old_deleted,new_ids,new_parents,new_deleted,expected",
    [
        (["a"], [], True, ["a"], [], False, False),
        (["a"], [], True, ["b"], [], True, False),
        (["a"], [], True, ["a", "b"], [], True, True),
        (["a"], [], False, [], [], False, True),
        ([], [], True, ["a"], [], True, False),
        ([], ["g1"], True, [], ["g2"], True, False),
        ([], ["g1"], True, [], ["g1", "g2"], True, True),
        (["a"], [], True, ["a"], ["g1"], True, False),
        (["a"], ["g1"], True, [], [], True, True),
    ],
)
def test_supersession_requires_query_filter_coverage(
    session,
    directory_seed,
    old_ids,
    old_parents,
    old_deleted,
    new_ids,
    new_parents,
    new_deleted,
    expected,
):
    old = _run(session, directory_seed)
    new = _run(session, directory_seed)
    _query(old, ids=old_ids, parents=old_parents, deleted=old_deleted)
    _query(new, ids=new_ids, parents=new_parents, deleted=new_deleted)
    new.published_version, new.status = 1, "COMPLETE"
    session.flush()
    assert directory_run_superseded(session, old) is expected


@pytest.mark.parametrize("changed", ["actor", "bc", "route", "tenant", "child"])
def test_published_coverage_cannot_cross_frozen_scope(session, directory_seed, changed):
    old = _run(session, directory_seed)
    new = _run(session, directory_seed)
    new.published_version, new.status = 1, "COMPLETE"
    session.flush()
    if changed == "actor":
        old.actor_id = uuid4()
    elif changed == "bc":
        old.bc_id = "other-bc"
    elif changed == "route":
        new.frozen_route = new.frozen_route | {"binding_revision": 99}
    elif changed == "tenant":
        # 不伪造跨租户数据库行；比较当前运行的冻结租户即可验证 SQL 隔离。
        old.tenant_id = uuid4()
    else:
        new.query = new.query | {"material_child_v1": {"source_run_id": str(uuid4())}}
    if changed not in {"actor", "bc", "tenant"}:
        session.flush()
    with session.no_autoflush:
        assert not directory_run_superseded(session, old)
    session.expire(old)


def test_older_overlapping_query_only_publishes_materials_of_winning_ad_observations(
    session, directory_seed
):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from sqlmodel import select

    from app.modules.ads.models import AdMaterialReference, AdObject
    from tests.modules.ads.test_material_directory_children import _source_page

    _authority(session, directory_seed)
    old = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    _query(old, ids=["a", "b"], deleted=True)
    new = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    _query(new, ids=["a"], deleted=True)
    now = datetime.now(UTC)

    def complete_page(ads, at, status):
        source = _source_page(
            directory_seed, 1, [(ad, "g") for ad in ads], terminal=True
        )
        return replace(
            source,
            items=tuple(
                replace(row, observed_at=at, operation_status=status)
                for row in source.items
            ),
            materials=tuple(
                replace(row, complete=True, operation_status=status)
                for row in source.materials
            ),
            materials_complete=True,
            material_missing_reason=None,
        )

    stage_directory_page(
        session,
        run_id=old.id,
        claim_generation=1,
        page=complete_page(["a", "b"], now - timedelta(minutes=2), "ENABLE"),
    )
    stage_directory_page(
        session,
        run_id=new.id,
        claim_generation=1,
        page=complete_page(["a"], now, "DISABLE"),
    )
    new_version = publish_directory(session, run_id=new.id, claim_generation=1)
    old_version = publish_directory(session, run_id=old.id, claim_generation=1)
    rows = session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == old.tenant_id
        )
    ).all()
    found = {row.ad_remote_id: row for row in rows}
    assert found["a"].operation_status == "DISABLE"
    assert found["a"].published_version == new_version
    assert found["b"].operation_status == "ENABLE"
    assert found["b"].published_version == old_version
    assert (
        session.get(
            AdObject, (old.tenant_id, old.advertiser_id, "ad", "a")
        ).operation_status
        == "DISABLE"
    )
    assert old.coverage == "MATERIALS_INCOMPLETE"
    assert old.missing_reason == "material_owner_observation_stale"


@pytest.mark.parametrize("different_material_id", [False, True])
def test_older_creative_scope_cannot_overwrite_newer_material_observation(
    session, directory_seed, different_material_id
):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import event
    from sqlmodel import select

    from app.integrations.tiktok.contracts.ads import EntityRef
    from app.modules.ads.models import AdMaterialReference
    from tests.modules.ads.test_material_directory_children import _source_page

    _authority(session, directory_seed)
    old = _run(session, directory_seed, kind="creative", ad_type="SMART_PLUS")
    _query(old, ids=["creative-a", "creative-b"], deleted=True)
    new = _run(session, directory_seed, kind="creative", ad_type="SMART_PLUS")
    _query(new, ids=["creative-a"], deleted=True)
    now = datetime.now(UTC)

    def page(ads, at, status):
        primary = _source_page(
            directory_seed, 1, [(ad, "g") for ad in ads], terminal=True
        )
        return replace(
            primary,
            items=tuple(
                replace(
                    obj,
                    ref=EntityRef(
                        obj.ref.tenant_id,
                        obj.ref.advertiser_id,
                        "creative",
                        f"creative-{obj.ref.remote_id}",
                    ),
                    parent_ref=obj.ref,
                    observed_at=at,
                    operation_status=status,
                )
                for obj in primary.items
            ),
            materials=tuple(
                replace(
                    row,
                    complete=True,
                    operation_status=status,
                    use_ref=replace(row.use_ref, ad_material_id="old-distinct-id")
                    if different_material_id
                    and status == "ENABLE"
                    and row.use_ref.ad_ref.remote_id == "a"
                    else row.use_ref,
                )
                for row in primary.materials
            ),
            materials_complete=True,
            material_missing_reason=None,
        )

    stage_directory_page(
        session,
        run_id=old.id,
        claim_generation=1,
        page=page(["a", "b"], now - timedelta(minutes=2), "ENABLE"),
    )
    stage_directory_page(
        session, run_id=new.id, claim_generation=1, page=page(["a"], now, "DISABLE")
    )
    new_version = publish_directory(session, run_id=new.id, claim_generation=1)
    reads = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "FROM ad_object" in statement
        ):
            reads.append(statement)

    event.listen(session.get_bind(), "before_cursor_execute", capture)
    try:
        old_version = publish_directory(session, run_id=old.id, claim_generation=1)
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", capture)
    rows = session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == old.tenant_id
        )
    ).all()
    found = {row.ad_remote_id: row for row in rows}
    assert len(rows) == 2
    assert found["a"].operation_status == "DISABLE"
    assert found["a"].published_version == new_version
    assert found["b"].published_version == old_version
    assert old.coverage == "MATERIALS_INCOMPLETE"
    assert old.missing_reason == "material_owner_observation_stale"
    # 仅已有的实体窄回读，不能增加素材过滤 SELECT 或逐创意懒加载。
    assert len([sql for sql in reads if "ad_object.parent_kind" in sql]) == 1
