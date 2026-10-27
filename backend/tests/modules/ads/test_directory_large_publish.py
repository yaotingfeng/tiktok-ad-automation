"""真实 PostgreSQL 大目录发布：保持全量原子可见，记录数据库与Python成本。"""

from dataclasses import replace
from time import monotonic

import pytest
from sqlalchemy import event, func
from sqlmodel import select

from app.integrations.tiktok.contracts.ads import (
    AdMaterialUsage,
    CallEvidence,
    DirectoryPage,
    EntityRef,
    MaterialUseRef,
)
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync import publish_directory, stage_directory_page
from tests.modules.ads.test_directory_sync import _authority, _page, _run


def test_large_full_directory_publish_preserves_every_object_and_material(
    session, directory_seed, record_property
):
    _authority(session, directory_seed)
    template = _page(directory_seed).items[0]
    # 一个真实大账户会由广告页带出大量创意和素材引用；不能只测千条广告。
    items = tuple(
        replace(
            template,
            ref=EntityRef(
                directory_seed.context.tenant_id, "account-a", "ad", f"large-{i}"
            ),
            configuration={"creative": "x" * 1024},
        )
        for i in range(12000)
    )
    materials = tuple(
        AdMaterialUsage(
            use_ref=MaterialUseRef(
                items[i % len(items)].ref, f"video-{i}", str(i), "VIDEO"
            ),
            local_material_id=None,
            operation_status="ENABLE",
            complete=True,
            name=f"material-{i}",
            creative_ids=(f"creative-{i}",),
        )
        for i in range(40000)
    )
    run = _run(session, directory_seed, kind="ad")
    stage_directory_page(
        session,
        run_id=run.id,
        page=DirectoryPage(items, materials, None, True, CallEvidence()),
        claim_generation=1,
    )
    stats = {
        "sql_count": 0,
        "sql_seconds": 0.0,
        "material_return_rows": 0,
        "wide_object_reads": 0,
    }
    connection = session.connection()

    def before(_conn, _cursor, _sql, _params, context, _many):
        context.test_started = monotonic()

    def after(_conn, cursor, sql, _params, context, _many):
        stats["sql_count"] += 1
        stats["sql_seconds"] += monotonic() - context.test_started
        if "INSERT INTO ad_material_reference" in sql and "RETURNING" in sql:
            stats["material_return_rows"] += cursor.rowcount
        if sql.startswith("SELECT") and "ad_object.configuration" in sql:
            stats["wide_object_reads"] += 1

    event.listen(connection, "before_cursor_execute", before)
    event.listen(connection, "after_cursor_execute", after)
    started = monotonic()
    try:
        version = publish_directory(session, run_id=run.id, claim_generation=1)
    finally:
        event.remove(connection, "before_cursor_execute", before)
        event.remove(connection, "after_cursor_execute", after)
    stats["total_seconds"] = monotonic() - started
    assert run.status == "COMPLETE"
    assert (
        session.exec(
            select(func.count())
            .select_from(AdObject)
            .where(
                AdObject.tenant_id == run.tenant_id,
                AdObject.published_version == version,
                AdObject.remote_id.startswith("large-"),
            )
        ).one()
        == 12000
    )
    assert (
        session.exec(
            select(func.count())
            .select_from(AdMaterialReference)
            .where(
                AdMaterialReference.tenant_id == run.tenant_id,
                AdMaterialReference.published_version == version,
            )
        ).one()
        == 40000
    )
    record_property("large_directory_publish", stats)
    assert stats["total_seconds"] < 45

    # 第二次走冲突更新和所有现有索引维护，不能只以首次插入证明大账户能发布。
    loaded = session.exec(
        select(AdMaterialReference)
        .where(AdMaterialReference.tenant_id == run.tenant_id)
        .limit(1)
    ).one()
    repeat = _run(session, directory_seed, kind="ad")
    stage_directory_page(
        session,
        run_id=repeat.id,
        page=DirectoryPage(items, materials, None, True, CallEvidence()),
        claim_generation=1,
    )
    started = monotonic()
    repeated_version = publish_directory(session, run_id=repeat.id, claim_generation=1)
    repeat_seconds = monotonic() - started
    assert repeat.status == "COMPLETE"
    assert loaded.published_version == repeated_version
    assert (
        session.exec(
            select(func.count())
            .select_from(AdMaterialReference)
            .where(
                AdMaterialReference.tenant_id == repeat.tenant_id,
                AdMaterialReference.published_version == repeated_version,
            )
        ).one()
        == 40000
    )
    record_property("large_directory_repeat_seconds", repeat_seconds)
    assert repeat_seconds < 45
    # 发布后的父链与系列名审计不需要再次传输上万条完整配置。
    assert stats["wide_object_reads"] == 0


def test_object_publish_refreshes_loaded_configuration_after_narrow_read(
    session, directory_seed
):
    from datetime import timedelta

    from app.modules.ads.sync import _entity_json, _publish_entities

    current = directory_seed.row
    original_version = current.published_version
    observed = current.observed_at + timedelta(seconds=1)
    incoming = replace(
        _page(directory_seed).items[0],
        ref=current.ref,
        observed_at=observed,
        name="更新后的广告",
        configuration={"creative": "新的配置"},
    )
    run = _run(session, directory_seed, kind="ad")
    published = _publish_entities(
        session,
        [_entity_json(incoming)],
        run=run,
        version=original_version + 1,
        fallback=observed,
    )
    assert published == [current]
    assert current.name == incoming.name
    assert current.configuration == incoming.configuration
    assert current.observed_at == observed
    assert current.published_version == original_version + 1


@pytest.mark.parametrize("expire_identity", [False, True])
def test_material_publish_refreshes_loaded_and_partly_expired_identity(
    session, directory_seed, expire_identity
):
    from app.modules.ads.sync import _publish_materials

    run = _run(session, directory_seed, kind="ad")
    row = AdMaterialReference(
        tenant_id=run.tenant_id,
        advertiser_id=run.advertiser_id,
        ad_remote_id="ad-loaded",
        platform_material_id="video-loaded",
        ad_material_id="non-null-id",
        material_type="VIDEO",
        name="before",
        complete=True,
        published_version=1,
    )
    session.add(row)
    session.flush()
    if expire_identity:
        session.expire(row, ["ad_material_id"])
    _publish_materials(
        session,
        [
            {
                "use_ref": {
                    "ad_ref": {
                        "tenant_id": str(run.tenant_id),
                        "advertiser_id": run.advertiser_id,
                        "kind": "ad",
                        "remote_id": "ad-loaded",
                    },
                    "platform_material_id": "video-loaded",
                    "ad_material_id": "non-null-id",
                    "material_type": "VIDEO",
                },
                "name": "after",
                "complete": True,
                "creative_ids": ["creative-new"],
            }
        ],
        run=run,
        version=2,
    )
    assert row.name == "after"
    assert row.published_version == 2
    assert row.ad_material_id == "non-null-id"
    assert row.creative_ids == ["creative-new"]
