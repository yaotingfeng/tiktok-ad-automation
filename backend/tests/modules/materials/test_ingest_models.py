"""Real PostgreSQL contracts for transient originals and ingest counters."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, event, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel

from app.core.db import engine
from app.models import User
from app.modules.materials.ingest_models import (
    IngestMilestone,
    IngestSession,
    IngestSessionFile,
    IngestTransition,
    ObjectBudget,
    ObjectCleanup,
    OriginalUse,
    SourceAccountLoad,
    TemporaryMaterialObject,
    canonical_object_key,
    record_milestone,
    transition_ingest_file,
)
from app.modules.materials.models import (
    AccountMaterial,
    MaterialFile,
    ObjectUpload,
    UploadBatch,
)
from app.modules.tenants.models import Tenant
from tests.modules.conftest import create_context
from tests.modules.materials.test_tenant_materials import mapping, material


def test_ingest_tables_are_migrated(session):
    names = set(inspect(session.connection()).get_table_names())
    assert {
        "ingest_session",
        "ingest_session_file",
        "temporary_material_object",
        "original_use",
        "object_cleanup",
        "object_budget",
        "ingest_milestone",
        "source_account_load",
    } <= names


def ingest_fixture(session, context, *, count=1):
    files = [
        material(session, context, f"ingest-{index}.mp4") for index in range(count)
    ]
    batch = IngestSession(
        tenant_id=context.tenant_id,
        bc_id="bc-a",
        actor_id=context.actor_id,
        request_id=uuid4(),
        request_digest="a" * 64,
        expected_files=count,
        expected_bytes=sum(file.byte_size for file in files),
    )
    session.add(batch)
    session.flush()
    rows = []
    for index, file in enumerate(files):
        row = IngestSessionFile(
            tenant_id=context.tenant_id,
            bc_id="bc-a",
            session_id=batch.id,
            client_index=index,
            material_id=file.id,
            byte_size=file.byte_size,
            manifest_digest="a" * 64,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return batch, rows, files


def original(session, context, file, *, generation=1, **values):
    row = TemporaryMaterialObject(
        tenant_id=context.tenant_id,
        bc_id=file.bc_id,
        material_id=file.id,
        generation=generation,
        object_key=canonical_object_key(
            context.tenant_id, file.bc_id, file.id, generation
        ),
        expected_bytes=file.byte_size,
        **values,
    )
    session.add(row)
    session.flush()
    return row


def mark(session, context, batch, file, milestone):
    return record_milestone(
        session,
        tenant_id=context.tenant_id,
        bc_id=batch.bc_id,
        session_id=batch.id,
        material_id=file.id,
        milestone=milestone,
    )


def test_client_index_is_unique_and_scope_rejects_foreign_references(
    session, context, other_context
):
    batch, rows, files = ingest_fixture(session, context)
    foreign_batch, _, foreign_files = ingest_fixture(session, other_context)
    row = rows[0]
    another = material(session, context, "another.mp4")
    with pytest.raises(IntegrityError) as error, session.begin_nested():
        session.add(
            IngestSessionFile(
                **{**row.model_dump(), "id": uuid4(), "material_id": another.id}
            )
        )
        session.flush()
    assert error.value.orig.diag.constraint_name == "uq_ingest_client_index"
    for field, value in (
        ("session_id", foreign_batch.id),
        ("material_id", foreign_files[0].id),
    ):
        with pytest.raises(IntegrityError) as error, session.begin_nested():
            session.add(
                IngestSessionFile(
                    **{
                        **row.model_dump(),
                        "id": uuid4(),
                        "client_index": 2,
                        "material_id": another.id,
                        field: value,
                    }
                )
            )
            session.flush()
        assert error.value.orig.sqlstate in {"23503", "23514"}
    with pytest.raises(IntegrityError), session.begin_nested():
        batch.actor_id = other_context.actor_id
        session.flush()
    obj = original(session, context, files[0])
    for cls, values in (
        (ObjectCleanup, {"reason": "test"}),
        (
            OriginalUse,
            {
                "actor_id": other_context.actor_id,
                "purpose": "validation",
                "expires_at": datetime.now(UTC) + timedelta(minutes=1),
            },
        ),
    ):
        with pytest.raises(IntegrityError) as error, session.begin_nested():
            session.add(
                cls(
                    tenant_id=other_context.tenant_id,
                    bc_id=obj.bc_id,
                    material_id=obj.material_id,
                    generation=obj.generation,
                    **values,
                )
            )
            session.flush()
        assert error.value.orig.sqlstate == "23503"


def test_generation_keys_and_storage_bindings_cannot_be_retargeted(session, context):
    file = material(session, context, "Moon.mp4")
    first = original(
        session,
        context,
        file,
        storage_provider="r2",
        storage_endpoint="https://test.invalid",
        storage_bucket="owned",
    )
    second = original(session, context, file, generation=2)
    assert first.object_key != second.object_key
    assert f"/bc/{file.bc_id}/" in first.object_key
    for values in (
        {"generation": 3},
        {"object_key": second.object_key},
        {"expected_bytes": 1},
        {"storage_bucket": "another"},
        {"storage_endpoint": "https://other.invalid"},
    ):
        with pytest.raises(IntegrityError) as error, session.begin_nested():
            session.execute(
                update(TemporaryMaterialObject)
                .where(TemporaryMaterialObject.id == first.id)
                .values(**values)
            )
        assert error.value.orig.sqlstate == "23514"


def test_cleanup_keeps_permanent_material_and_platform_mapping(session, context):
    file = material(session, context, "Moon.mp4")
    asset = mapping(session, context, file)
    obj = original(
        session,
        context,
        file,
        status="verified",
        actual_bytes=file.byte_size,
        sha256="a" * 64,
        video_md5="b" * 32,
        digest_verified_at=datetime.now(UTC),
    )
    file.current_object_generation = obj.generation
    session.flush()
    assert file.original_available
    obj.status = "deleted"
    session.flush()
    session.expire_all()
    assert not file.original_available
    assert session.get(AccountMaterial, asset.id).video_id == asset.video_id
    session.delete(obj)
    session.flush()
    assert session.get(MaterialFile, file.id) is not None
    assert session.get(AccountMaterial, asset.id).status == "available"
    assert not file.original_available


def test_verified_original_requires_trusted_digest_and_length(session, context):
    file = material(session, context, "Moon.mp4")
    obj = original(session, context, file)
    with pytest.raises(IntegrityError) as error, session.begin_nested():
        obj.status = "verified"
        session.flush()
    assert error.value.orig.diag.constraint_name == "ck_temporary_object_verified"


def test_duplicate_facts_retry_and_late_revisions_conserve_counters(session, context):
    batch, rows, files = ingest_fixture(session, context)
    row, file = rows[0], files[0]
    assert mark(session, context, batch, file, "accepted")
    assert not mark(session, context, batch, file, "accepted")
    assert mark(session, context, batch, file, "uploaded")
    assert transition_ingest_file(
        session,
        tenant_id=context.tenant_id,
        file_id=row.id,
        expected_revision=0,
        status="failed",
    )
    session.refresh(batch)
    assert batch.failed_count == 1
    assert transition_ingest_file(
        session,
        tenant_id=context.tenant_id,
        file_id=row.id,
        expected_revision=1,
        status="uploading",
    )
    assert not transition_ingest_file(
        session,
        tenant_id=context.tenant_id,
        file_id=row.id,
        expected_revision=0,
        status="failed",
    )
    assert transition_ingest_file(
        session,
        tenant_id=context.tenant_id,
        file_id=row.id,
        expected_revision=2,
        status="available",
    )
    assert mark(session, context, batch, file, "ready")
    assert not mark(session, context, batch, file, "ready")
    assert not transition_ingest_file(
        session,
        tenant_id=context.tenant_id,
        file_id=row.id,
        expected_revision=2,
        status="failed",
    )
    session.refresh(batch)
    assert (
        batch.accepted_count,
        batch.uploaded_count,
        batch.ready_count,
        batch.failed_count,
    ) == (1, 1, 1, 0)
    assert (batch.accepted_bytes, batch.uploaded_bytes, batch.ready_bytes) == (
        file.byte_size,
    ) * 3
    transitions = (
        session.execute(
            select(IngestTransition).where(IngestTransition.file_id == row.id)
        )
        .scalars()
        .all()
    )
    assert len(transitions) == 3


def test_two_generations_cleanup_receipts_do_not_count_file_twice(session, context):
    batch, rows, files = ingest_fixture(session, context)
    file = files[0]
    first = original(session, context, file)
    second = original(session, context, file, generation=2)
    file.current_object_generation = 2
    for name in ("accepted", "uploaded", "ready"):
        mark(session, context, batch, file, name)
    first.status = "deleted"
    assert mark(session, context, batch, file, "cleaned")
    second.status = "verified"
    second.actual_bytes = file.byte_size
    second.sha256 = "a" * 64
    second.video_md5 = "b" * 32
    second.digest_verified_at = datetime.now(UTC)
    session.flush()
    assert file.original_available
    second.status = "deleted"
    assert not mark(session, context, batch, file, "cleaned")
    assert not mark(session, context, batch, file, "cleaned")
    session.refresh(batch)
    assert batch.cleaned_count == 1
    assert batch.cleaned_bytes == file.byte_size
    assert batch.ready_count == 1
    assert not file.original_available


def test_budgets_are_global_and_per_tenant_not_bc_scoped(session, context):
    session.add(
        ObjectBudget(
            scope_key=f"tenant:{context.tenant_id}", tenant_id=context.tenant_id
        )
    )
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(ObjectBudget(scope_key="tenant:wrong", tenant_id=context.tenant_id))
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ObjectBudget(
                scope_key=f"tenant:{context.tenant_id}", tenant_id=context.tenant_id
            )
        )
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(ObjectBudget)
            .where(ObjectBudget.scope_key == f"tenant:{context.tenant_id}")
            .values(reserved_bytes=-1)
        )


def test_source_load_is_separate_from_session_and_tenant_scoped(
    session, context, other_context
):
    batch, _, files = ingest_fixture(session, context)
    asset = mapping(session, context, files[0])
    row = SourceAccountLoad(
        tenant_id=context.tenant_id,
        bc_id="bc-a",
        advertiser_id=asset.advertiser_id,
        connection_id=asset.connection_id,
    )
    session.add(row)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        row.tenant_id = other_context.tenant_id
        session.flush()
    assert "session_id" not in SourceAccountLoad.__table__.c
    assert batch.accepted_count == 0


def test_upgrade_preserves_legacy_ids_keys_multipart_and_mapping(session, context):
    files = [
        material(session, context, f"legacy-{state}.mp4", state=state)
        for state in ("receiving", "stored", "unavailable")
    ]
    asset = mapping(session, context, files[1])
    batch = UploadBatch(
        tenant_id=context.tenant_id,
        bc_id="bc-a",
        actor_id=context.actor_id,
        request_id=uuid4(),
        request_digest="a" * 64,
    )
    session.add(batch)
    session.flush()
    session.add(
        ObjectUpload(
            tenant_id=context.tenant_id,
            bc_id="bc-a",
            material_id=files[0].id,
            batch_id=batch.id,
            object_key=files[0].object_key,
            expected_size=files[0].byte_size,
            s3_upload_id="test-multipart",
            parts=[{"part_number": 1, "etag": "test"}],
        )
    )
    session.flush()
    identities = [(file.id, file.object_key, file.storage_state) for file in files]
    old_asset_id, old_vid = asset.id, asset.video_id
    migration = import_module(
        "app.alembic.versions.r2_transient_ingest_persist_transient_originals_and_ingest_"
    )
    with Operations.context(MigrationContext.configure(session.connection())):
        migration.downgrade()
        migration.upgrade()
    session.expire_all()
    for identity, key, state in identities:
        file = session.get(MaterialFile, identity)
        assert file.object_key == key
        assert file.storage_state == state
        assert file.current_object_generation == 1
        obj = file.current_object
        assert obj.object_key == key
        assert obj.status == {"unavailable": "missing"}.get(state, state)
        assert obj.storage_provider is None and obj.storage_bucket is None
        assert obj.digest_verified_at is None
        assert not file.original_available
        if state == "receiving":
            assert obj.s3_upload_id == "test-multipart"
            assert obj.parts == [{"part_number": 1, "etag": "test"}]
    assert session.get(AccountMaterial, old_asset_id).video_id == old_vid
    budget = session.get(ObjectBudget, f"tenant:{context.tenant_id}")
    assert budget.reserved_bytes == files[0].byte_size + files[1].byte_size
    assert budget.stored_bytes == files[1].byte_size
    # Reverting metadata preserves the original legacy receiving/stored states.
    with Operations.context(MigrationContext.configure(session.connection())):
        migration.downgrade()
    records = session.execute(
        text(
            "SELECT id, object_key, storage_state FROM material_file WHERE tenant_id = :tenant"
        ),
        {"tenant": context.tenant_id},
    ).all()
    assert set(records) == set(identities)
    assert (
        session.execute(
            text("SELECT video_id FROM account_material WHERE id = :id"),
            {"id": old_asset_id},
        ).scalar_one()
        == old_vid
    )
    # Keep schema consistent for the outer test transaction's cleanup.
    with Operations.context(MigrationContext.configure(session.connection())):
        migration.upgrade()


def test_hundred_completions_use_indexed_constant_work_without_lost_updates():
    with Session(engine) as setup:
        owner = create_context(setup)
        batch, rows, files = ingest_fixture(setup, owner, count=100)
        for file in files:
            mark(setup, owner, batch, file, "accepted")
        session_id = batch.id
        identities = [(row.id, file.id) for row, file in zip(rows, files, strict=True)]
        expected_bytes = batch.expected_bytes
        setup.commit()
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    def finish(identity):
        row_id, material_id = identity
        with Session(engine) as worker:
            assert transition_ingest_file(
                worker,
                tenant_id=owner.tenant_id,
                file_id=row_id,
                expected_revision=0,
                status="available",
            )
            assert record_milestone(
                worker,
                tenant_id=owner.tenant_id,
                bc_id="bc-a",
                session_id=session_id,
                material_id=material_id,
                milestone="ready",
            )
            worker.commit()

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(finish, identities))
        with Session(engine) as verify:
            batch = verify.get(IngestSession, session_id)
            assert batch.ready_count == 100
            assert batch.ready_bytes == expected_bytes
            assert (
                verify.execute(
                    select(IngestMilestone).where(
                        IngestMilestone.session_id == session_id,
                        IngestMilestone.milestone == "ready",
                    )
                )
                .scalars()
                .all()
                .__len__()
                == 100
            )
        # No rescans or aggregation: only one file lookup and atomic header delta.
        ingest_reads = [
            sql
            for sql in statements
            if sql.lstrip().upper().startswith("SELECT")
            and "ingest_session_file" in sql
        ]
        assert len(ingest_reads) == 200
        assert all(
            "WHERE" in sql and ("id =" in sql or "material_id =" in sql)
            for sql in ingest_reads
        )
        assert not any(
            "COUNT(" in sql.upper() or "SUM(" in sql.upper() for sql in statements
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        with Session(engine) as cleanup:
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    cleanup.execute(
                        delete(table).where(table.c.tenant_id == owner.tenant_id)
                    )
            cleanup.execute(delete(Tenant).where(Tenant.id == owner.tenant_id))
            cleanup.execute(delete(User).where(User.id == owner.actor_id))
            cleanup.commit()


def test_manifest_identity_and_generation_are_fenced(session, context):
    _, rows, _ = ingest_fixture(session, context)
    row = rows[0]
    for field, value in (
        ("manifest_digest", "b" * 64),
        ("byte_size", 123),
        ("client_index", 42),
    ):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                update(IngestSessionFile)
                .where(IngestSessionFile.id == row.id)
                .values({field: value})
            )
    row.current_generation = 2
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        row.current_generation = 1
        session.flush()


@pytest.mark.parametrize("actual_bytes", [None, 123])
def test_verified_original_rejects_missing_or_mismatched_length_with_hashes(
    session, context, actual_bytes
):
    file = material(session, context, "verified-length.mp4")
    with pytest.raises(IntegrityError) as error, session.begin_nested():
        original(
            session,
            context,
            file,
            status="verified",
            actual_bytes=actual_bytes,
            sha256="a" * 64,
            video_md5="b" * 32,
            digest_verified_at=datetime.now(UTC),
        )
    assert error.value.orig.diag.constraint_name == "ck_temporary_object_verified"


@pytest.mark.parametrize(
    "object_state",
    [None, "previous_generation_only", "deleted", "cleanup_pending", "missing"],
)
def test_downgrade_missing_or_cleaned_current_original_remains_unavailable(
    session, context, object_state
):
    file = material(session, context, "downgrade-lost.mp4")
    legacy = material(session, context, "legacy-no-generation.mp4")
    asset = mapping(session, context, file)
    asset.mid = "retained-mid"
    obj = original(
        session,
        context,
        file,
        status="stored"
        if object_state == "previous_generation_only"
        else object_state or "deleted",
    )
    file.current_object_generation = (
        2 if object_state == "previous_generation_only" else obj.generation
    )
    session.flush()
    if object_state is None:
        session.delete(obj)
        session.flush()
    identities = file.id, asset.id, asset.video_id, asset.mid, legacy.id
    assert not file.original_available
    migration = import_module(
        "app.alembic.versions.r2_transient_ingest_persist_transient_originals_and_ingest_"
    )
    with Operations.context(MigrationContext.configure(session.connection())):
        migration.downgrade()
    file_id, asset_id, video_id, mid, legacy_id = identities
    assert (
        session.execute(
            text("SELECT storage_state FROM material_file WHERE id = :id"),
            {"id": file_id},
        ).scalar_one()
        == "unavailable"
    )
    assert session.execute(
        text("SELECT material_id, video_id, mid FROM account_material WHERE id = :id"),
        {"id": asset_id},
    ).one() == (file_id, video_id, mid)
    assert (
        session.execute(
            text("SELECT storage_state FROM material_file WHERE id = :id"),
            {"id": legacy_id},
        ).scalar_one()
        == "stored"
    )
    with Operations.context(MigrationContext.configure(session.connection())):
        migration.upgrade()


def test_session_occupancy_is_nonnegative_and_stored_is_subset(session, context):
    batch, _, _ = ingest_fixture(session, context)
    for reserved, stored in ((-1, 0), (0, -1), (10, 11)):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text(
                    "UPDATE ingest_session SET reserved_bytes = :reserved, stored_bytes = :stored WHERE id = :id"
                ),
                {"id": batch.id, "reserved": reserved, "stored": stored},
            )
    session.execute(
        text(
            "UPDATE ingest_session SET reserved_bytes = 123, stored_bytes = 100 WHERE id = :id"
        ),
        {"id": batch.id},
    )
    assert session.execute(
        text("SELECT reserved_bytes, stored_bytes FROM ingest_session WHERE id = :id"),
        {"id": batch.id},
    ).one() == (123, 100)
