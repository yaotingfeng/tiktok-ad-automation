from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel

from app.core.db import engine
from app.core.errors import DomainError
from app.models import User
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialFile,
)
from app.modules.materials.service import match_materials
from app.modules.tenants.models import Tenant
from tests.modules.conftest import create_context


def material(session, context, name, *, bc="bc-a", state="stored"):
    if session.get(TenantBC, (context.tenant_id, bc)) is None:
        session.add(TenantBC(tenant_id=context.tenant_id, bc_id=bc))
        session.flush()
    identity = uuid4()
    row = MaterialFile(
        id=identity,
        tenant_id=context.tenant_id,
        bc_id=bc,
        file_name=name,
        object_key=f"tenants/{context.tenant_id}/materials/{identity}/original",
        byte_size=4_000_000_000,
        storage_state=state,
    )
    session.add(row)
    session.flush()
    return row


def mapping(session, context, file, *, account="account-a"):
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add_all(
        [
            connection,
            AdvertiserAccount(tenant_id=context.tenant_id, advertiser_id=account),
        ]
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=context.tenant_id,
            bc_id=file.bc_id,
            advertiser_id=account,
            connection_id=connection.id,
            in_bc=True,
            authorized=True,
            active=True,
        )
    )
    session.flush()
    row = AccountMaterial(
        tenant_id=context.tenant_id,
        bc_id=file.bc_id,
        material_id=file.id,
        advertiser_id=account,
        connection_id=connection.id,
        video_id=f"video-{account}",
        status="available",
        verified_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def test_full_material_pagination_uses_stable_names_ids_and_tenant_bc(
    session, context, other_context
):
    expected = [
        material(session, context, f"Moon_{index // 3:03}.mp4") for index in range(205)
    ]
    material(session, other_context, "Moon.mp4")
    material(session, context, "Moon.mp4", bc="bc-b")
    material(session, context, "Moon_receiving.mp4", state="receiving")
    material(session, context, "Moon_lost.mp4", state="unavailable")
    cursor = None
    seen, sizes = [], []
    while True:
        page = match_materials(
            session, context=context, bc_id="bc-a", title="Moon", cursor=cursor
        )
        seen.extend(item.material_id for item in page.items)
        sizes.append(len(page.items))
        cursor = page.next_cursor
        if not cursor:
            break
    assert sizes == [100, 100, 5]
    assert seen == [
        row.id
        for row in sorted(expected, key=lambda row: (row.file_name.encode(), row.id))
    ]
    assert len(set(seen)) == 205


@pytest.mark.parametrize("title", ["Moon 100%", "Moon_", "Moon\\", "Straße"])
def test_database_literal_matching_agrees_with_unicode_casefold(
    session, context, title
):
    wanted = material(session, context, title.upper() + "_01.mp4")
    material(session, context, "MoonX1000.mp4")
    result = match_materials(
        session, context=context, bc_id="bc-a", title=" " + title + " "
    )
    assert [item.material_id for item in result.items] == [wanted.id]
    assert wanted.file_name_folded == wanted.file_name.casefold()


def test_one_material_can_match_two_drama_titles_without_mutation(session, context):
    file = material(session, context, "Moon-Sun.mp4")
    for title in ("Moon", "Sun"):
        assert (
            match_materials(session, context=context, bc_id="bc-a", title=title)
            .items[0]
            .material_id
            == file.id
        )
    assert "drama_id" not in file.model_dump()


def test_cursor_binds_material_query_and_rejects_tampering(
    session, context, other_context
):
    for index in range(101):
        material(session, context, f"Moon_{index}.mp4")
    material(session, context, "Moon.mp4", bc="bc-b")
    material(session, other_context, "Moon.mp4")
    cursor = match_materials(
        session, context=context, bc_id="bc-a", title="Moon"
    ).next_cursor
    assert cursor
    for overrides in (
        {"context": other_context},
        {"bc_id": "bc-b"},
        {"title": "Sun"},
        {"cursor": cursor[:-4] + "AAAA"},
        {"cursor": "bad"},
    ):
        with pytest.raises(DomainError) as error:
            match_materials(
                session,
                **{
                    "context": context,
                    "bc_id": "bc-a",
                    "title": "Moon",
                    "cursor": cursor,
                    **overrides,
                },
            )
        assert error.value.code == "invalid_cursor"
    with pytest.raises(DomainError) as error:
        match_materials(session, context=context, bc_id="bc-a", title=" ")
    assert error.value.code == "empty_title"


def test_verified_assets_allow_missing_original_and_keep_candidates_bounded(
    session, context
):
    file = material(session, context, "Moon.mp4", state="unavailable")
    assert (
        match_materials(session, context=context, bc_id="bc-a", title="Moon").items
        == []
    )
    first = mapping(session, context, file)
    mapping(session, context, file, account="account-b")
    item = match_materials(session, context=context, bc_id="bc-a", title="Moon").items[
        0
    ]
    assert not item.original_available
    assert [asset.asset_id for asset in item.source_assets] == [first.id]


def test_account_mapping_rejects_cross_tenant_material_and_unverified_available(
    session, context, other_context
):
    file = material(session, context, "Moon.mp4")
    foreign = material(session, other_context, "Moon.mp4")
    existing = mapping(session, context, file)
    for overrides, sqlstate in (
        ({"material_id": foreign.id}, "23503"),
        ({"verified_at": None}, "23514"),
        ({"video_id": ""}, "23514"),
    ):
        with pytest.raises(IntegrityError) as error, session.begin_nested():
            for key, value in overrides.items():
                setattr(existing, key, value)
            session.flush()
        assert error.value.orig.sqlstate == sqlstate


def test_unverified_operation_blocks_new_path_until_terminal(session, context):
    file = material(session, context, "Moon.mp4")
    asset = mapping(session, context, file)
    values = {
        "tenant_id": context.tenant_id,
        "bc_id": "bc-a",
        "material_id": file.id,
        "advertiser_id": asset.advertiser_id,
        "request_digest": "a" * 64,
    }
    first = MaterialAssetOperation(
        **values, path="upload_original", status="result_unknown"
    )
    session.add(first)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(MaterialAssetOperation(**values, path="share_source"))
        session.flush()
    first.status = "succeeded"
    session.flush()
    session.add(MaterialAssetOperation(**values, path="share_source"))
    session.flush()


def test_concurrent_operations_cannot_bypass_target_lock_with_another_path():
    with Session(engine) as setup:
        owner = create_context(setup)
        file = material(setup, owner, "Moon.mp4")
        asset = mapping(setup, owner, file)
        values = {
            "tenant_id": owner.tenant_id,
            "bc_id": file.bc_id,
            "material_id": file.id,
            "advertiser_id": asset.advertiser_id,
            "request_digest": "a" * 64,
        }
        setup.commit()
    gate = Barrier(2)

    def claim(path):
        with Session(engine) as worker:
            gate.wait(timeout=5)
            worker.add(MaterialAssetOperation(**values, path=path))
            try:
                worker.commit()
                return True
            except IntegrityError as error:
                worker.rollback()
                assert (
                    error.orig.diag.constraint_name
                    == "uq_material_unverified_operation"
                )
                return False

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            assert sorted(workers.map(claim, ["upload_original", "share_source"])) == [
                False,
                True,
            ]
    finally:
        with Session(engine) as cleanup:
            # This test committed its own tenant so both real workers can see it.
            # Remove only that tenant's fixtures in dependency order.
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    cleanup.execute(
                        delete(table).where(table.c.tenant_id == owner.tenant_id)
                    )
            cleanup.execute(delete(Tenant).where(Tenant.id == owner.tenant_id))
            cleanup.execute(delete(User).where(User.id == owner.actor_id))
            cleanup.commit()


@pytest.mark.parametrize("value", [" ", "\t\n", "\u00a0", "\u202f", "\u3000"])
def test_available_video_identity_rejects_all_python_whitespace(
    session, context, value
):
    file = material(session, context, "Moon.mp4")
    asset = mapping(session, context, file)
    with pytest.raises(IntegrityError) as error, session.begin_nested():
        asset.video_id = value
        session.flush()
    assert error.value.orig.sqlstate == "23514"


def test_matching_reloads_storage_and_mapping_facts_in_existing_session(
    session, context
):
    file = material(session, context, "Moon.mp4")
    asset = mapping(session, context, file)
    assert (
        match_materials(session, context=context, bc_id="bc-a", title="Moon")
        .items[0]
        .original_available
    )
    session.execute(
        update(MaterialFile)
        .where(MaterialFile.id == file.id)
        .values(storage_state="unavailable"),
        execution_options={"synchronize_session": False},
    )
    session.execute(
        update(AccountMaterial)
        .where(AccountMaterial.id == asset.id)
        .values(video_id="new-verified-video"),
        execution_options={"synchronize_session": False},
    )
    item = match_materials(session, context=context, bc_id="bc-a", title="Moon").items[
        0
    ]
    assert item.original_available is False
    assert item.source_assets[0].video_id == "new-verified-video"
