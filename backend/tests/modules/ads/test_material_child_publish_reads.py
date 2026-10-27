"""素材过滤复用已发布对象的窄回读，不能重复读创意或触发逐对象懒加载。"""

from dataclasses import replace
from datetime import timedelta

from sqlalchemy import event
from sqlmodel import select

from app.core.config import settings
from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage, EntityRef
from app.modules.ads import material_directory_children
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync import publish_directory, stage_directory_page
from app.modules.ads.sync_models import AdDirectoryRun
from tests.modules.ads.test_directory_sync import _authority, _run
from tests.modules.ads.test_material_directory_children import _source_page


def test_mixed_child_versions_reuse_loaded_objects_without_extra_creative_reads(
    session, directory_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    _authority(session, directory_seed)
    source = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    primary = _source_page(directory_seed, 1, [("ad-a", "group")], terminal=True)
    owner = primary.items[0]
    # 跨过500行批次边界，同VID但独立素材/创意身份；不能因ID不存在而放过旧观察。
    materials = tuple(
        replace(
            primary.materials[0],
            use_ref=replace(
                primary.materials[0].use_ref,
                platform_material_id="shared-video",
                ad_material_id=f"material-{i}",
            ),
            creative_ids=(f"creative-{i}",),
        )
        for i in range(501)
    )
    stage_directory_page(
        session,
        run_id=source.id,
        claim_generation=1,
        page=replace(primary, materials=materials),
    )
    publish_directory(session, run_id=source.id, claim_generation=1)
    child = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == source.tenant_id,
            AdDirectoryRun.kind == "creative",
        )
    ).one()
    creatives = tuple(
        replace(
            owner,
            ref=EntityRef(
                owner.ref.tenant_id,
                owner.ref.advertiser_id,
                "creative",
                f"creative-{i}",
            ),
            parent_ref=owner.ref,
            observed_at=owner.observed_at + timedelta(seconds=1),
        )
        for i in range(501)
    )
    stage_directory_page(
        session,
        run_id=child.id,
        claim_generation=1,
        page=DirectoryPage(
            creatives,
            tuple(replace(m, complete=True, name="child") for m in materials),
            None,
            True,
            CallEvidence(),
        ),
    )
    session.add(
        AdObject(
            tenant_id=source.tenant_id,
            advertiser_id=source.advertiser_id,
            kind="creative",
            remote_id="creative-0",
            parent_kind="ad",
            parent_remote_id="ad-a",
            ad_type="SMART_PLUS",
            name="newer",
            configuration={},
            operation_status="DISABLE",
            observed_at=owner.observed_at + timedelta(hours=1),
            published_version=999,
            source_connection_id=source.connection_id,
            source_channel=source.channel,
        )
    )
    newer = session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == source.tenant_id,
            AdMaterialReference.ad_material_id == "material-0",
        )
    ).one()
    newer.ad_material_id = "newer-material-0"
    newer.complete, newer.name, newer.published_version = True, "newer", 999
    tenant_id, child_id = source.tenant_id, child.id
    session.flush()
    session.expunge_all()  # 测试不能借先前ORM缓存掩盖published_version懒加载。
    original_filter = material_directory_children.filter_current_child_materials
    object_reads = []

    def observe_filter(*args, **kwargs):
        connection = args[0].connection()

        def record(_connection, _cursor, statement, _parameters, _context, _many):
            if (
                statement.lstrip().upper().startswith("SELECT")
                and "FROM ad_object" in statement
            ):
                object_reads.append(statement)

        event.listen(connection, "before_cursor_execute", record)
        try:
            return original_filter(*args, **kwargs)
        finally:
            event.remove(connection, "before_cursor_execute", record)

    monkeypatch.setattr(
        material_directory_children, "filter_current_child_materials", observe_filter
    )
    publish_directory(session, run_id=child_id, claim_generation=1)
    rows = session.exec(
        select(AdMaterialReference).where(AdMaterialReference.tenant_id == tenant_id)
    ).all()
    assert len(rows) == 501 and all(row.complete for row in rows)
    by_id = {row.ad_material_id: row for row in rows}
    assert "material-0" not in by_id
    assert by_id["newer-material-0"].name == "newer"
    assert by_id["newer-material-0"].published_version == 999
    assert all(by_id[f"material-{i}"].name == "child" for i in range(1, 501))
    child = session.get(AdDirectoryRun, child_id)
    assert child is not None and child.coverage == "MATERIALS_INCOMPLETE"
    # 过滤仅核对一次主ad观察；创意版本已随发布读回，不准追加创意查询/N+1。
    assert len(object_reads) == 1
    assert "observed_at" in object_reads[0]
    assert "published_version" not in object_reads[0]
