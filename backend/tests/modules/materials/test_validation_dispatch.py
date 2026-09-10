"""Trusted byte validation and transactional stage handoff; no live storage calls."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, SQLModel, select

from app.core.config import settings
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.models import User
from app.modules.materials.ingest_models import (
    IngestSession,
    IngestSessionFile,
    OriginalUse,
    TemporaryMaterialObject,
    record_milestone,
)
from app.modules.materials.models import MaterialAssetOperation, MaterialFile
from app.modules.materials.object_validation import (
    enqueue_validation,
    repair_validations,
    validate_original,
)
from app.modules.tenants.models import Tenant
from tests.modules.conftest import create_context
from tests.modules.materials.test_ingest_models import original
from tests.modules.materials.test_tenant_materials import mapping, material


def ingest_fixture(session, context):
    file = material(session, context, "Moon-video.mp4")
    file.byte_size = 5
    session.flush()
    batch = IngestSession(
        tenant_id=context.tenant_id,
        bc_id=file.bc_id,
        actor_id=context.actor_id,
        request_id=uuid4(),
        request_digest="a" * 64,
        expected_files=1,
        expected_bytes=5,
    )
    session.add(batch)
    session.flush()
    row = IngestSessionFile(
        tenant_id=context.tenant_id,
        bc_id=file.bc_id,
        session_id=batch.id,
        client_index=0,
        material_id=file.id,
        byte_size=5,
        manifest_digest="a" * 64,
    )
    session.add(row)
    session.flush()
    record_milestone(
        session,
        tenant_id=context.tenant_id,
        bc_id=file.bc_id,
        session_id=batch.id,
        material_id=file.id,
        milestone="accepted",
    )
    return batch, [row], [file]


@pytest.fixture
def validation_case():
    with Session(engine) as session:
        context = create_context(session)
        batch, files, materials = ingest_fixture(session, context)
        file = materials[0]
        obj = original(
            session,
            context,
            file,
            storage_provider="r2",
            storage_endpoint="https://test.r2.cloudflarestorage.com",
            storage_bucket="private-test",
        )
        file.current_object_generation = 1
        # Compact bytes fixture updates immutable manifest once, before generation exists.
        # Use matching fake repeated stream whose expected size remains the real manifest.
        obj.status, obj.actual_bytes = "stored", obj.expected_bytes
        obj.received_at = datetime.now(UTC)
        dispatch_id = enqueue_validation(session, context=context, object_id=obj.id)
        ids = context, obj.id, file.id, batch.id, files[0].id, dispatch_id
        session.commit()
    try:
        yield ids
    finally:
        with Session(engine) as cleanup, cleanup.begin():
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    cleanup.exec(
                        delete(table).where(table.c.tenant_id == context.tenant_id)
                    )
            cleanup.exec(delete(Tenant).where(Tenant.id == context.tenant_id))
            cleanup.exec(delete(User).where(User.id == context.actor_id))


class FakeStorage:
    def __init__(self, obj, material, *, body=b"video", corrupt=False, on_read=None):
        self.obj, self.material = obj, material
        self.body, self.corrupt, self.on_read = body, corrupt, on_read
        self.reads = self.signs = 0

    def head_object(self, **kwargs):
        assert kwargs == {"Bucket": self.obj.storage_bucket, "Key": self.obj.object_key}
        return {
            "ContentLength": self.obj.expected_bytes,
            "ContentType": self.material.mime_type,
            "Metadata": {
                "tenant-id": str(self.obj.tenant_id),
                "bc-id": self.obj.bc_id,
                "material-id": str(self.obj.material_id),
                "generation": str(self.obj.generation),
            },
        }

    def get_object(self, **kwargs):
        self.reads += 1
        if self.on_read:
            self.on_read()
        return {**self.head_object(**kwargs), "Body": BytesIO(self.body)}

    def generate_presigned_url(self, method, **kwargs):
        self.signs += 1
        assert kwargs["Params"] == {
            "Bucket": self.obj.storage_bucket,
            "Key": self.obj.object_key,
        }
        return "https://private.invalid/object?private-test-signature"


def test_completion_enqueues_exact_validator_once(session, context):
    batch, files, materials = ingest_fixture(session, context)
    obj = original(session, context, materials[0], status="stored")
    materials[0].current_object_generation = obj.generation
    session.flush()
    first = enqueue_validation(session, context=context, object_id=obj.id)
    assert first == enqueue_validation(session, context=context, object_id=obj.id)
    row = session.get(PendingDispatch, first)
    assert row.task_name == "materials.validate_original"
    assert row.payload["object_id"] == str(obj.id)
    assert row.payload["generation"] == 1
    assert not session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_name == "materials.upload_original",
            PendingDispatch.tenant_id == context.tenant_id,
        )
    ).first()


def test_corrupt_stream_never_marks_trusted_or_enqueues_source(
    validation_case, monkeypatch
):
    context, object_id, material_id, _, _, dispatch_id = validation_case
    monkeypatch.setattr(settings, "MATERIAL_URL_MAX_UPLOAD_BYTES", 5_000_000_000)
    with Session(engine) as session:
        obj, file = (
            session.get(TemporaryMaterialObject, object_id),
            session.get(MaterialFile, material_id),
        )
        transport = FakeStorage(obj, file, body=b"bad")
        payload = session.get(PendingDispatch, dispatch_id).payload
    validate_original(
        database_engine=engine,
        context=context,
        object_id=object_id,
        dispatch_id=dispatch_id,
        generation=1,
        revision=payload["revision"],
        s3=transport,
    )
    with Session(engine) as session:
        obj = session.get(TemporaryMaterialObject, object_id)
        assert obj.digest_verified_at is None
        assert obj.error_code == "incomplete_object"
        assert not session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id,
                PendingDispatch.task_name == "materials.upload_original",
            )
        ).first()
        assert all(
            use.status == "released"
            for use in session.exec(
                select(OriginalUse).where(OriginalUse.tenant_id == context.tenant_id)
            ).all()
        )


def test_repair_retains_dispatch_identity_and_original_backoff(validation_case):
    context, object_id, _, _, _, dispatch_id = validation_case
    due = datetime.now(UTC) - timedelta(seconds=10)
    with Session(engine) as session, session.begin():
        dispatch = session.get(PendingDispatch, dispatch_id)
        dispatch.published_at = datetime.now(UTC) - timedelta(minutes=20)
        dispatch.available_at = due
    with Session(engine) as session, session.begin():
        assert repair_validations(session) >= 1
    with Session(engine) as session:
        row = session.get(PendingDispatch, dispatch_id)
        assert row.published_at is None
        assert row.available_at == due


def test_ingest_url_requires_current_trusted_object_and_pins_namespace(
    session, context
):
    batch, files, materials = ingest_fixture(session, context)
    obj = original(
        session,
        context,
        materials[0],
        status="stored",
        storage_provider="r2",
        storage_endpoint="https://test.r2.cloudflarestorage.com",
        storage_bucket="private-test",
    )
    materials[0].current_object_generation = obj.generation
    session.flush()
    # Signing orchestration uses an independently committed transaction; its inner
    # helper is tested with the same session for deterministic failure below.
    from app.modules.materials.object_validation import issue_ingest_url

    transport = FakeStorage(obj, materials[0])
    with pytest.raises(DomainError):
        issue_ingest_url(
            session,
            context=context,
            object_id=obj.id,
            operation_id=uuid4(),
            s3=transport,
        )
    assert transport.signs == 0
    obj.status, obj.actual_bytes = "verified", obj.expected_bytes
    obj.sha256, obj.video_md5 = "a" * 64, "b" * 32
    obj.digest_verified_at = datetime.now(UTC)
    session.flush()
    asset = mapping(session, context, materials[0])
    operation = MaterialAssetOperation(
        tenant_id=obj.tenant_id,
        bc_id=obj.bc_id,
        material_id=obj.material_id,
        advertiser_id=asset.advertiser_id,
        path="upload_original",
        status="sending",
        attempt_token=uuid4(),
        request_digest="a" * 64,
    )
    session.add(operation)
    session.flush()
    url = issue_ingest_url(
        session,
        context=context,
        object_id=obj.id,
        operation_id=operation.id,
        s3=transport,
    )
    assert "private-test-signature" in url
    use = session.exec(
        select(OriginalUse).where(OriginalUse.tenant_id == context.tenant_id)
    ).one()
    assert "private-test-signature" not in repr(use)


def test_trusted_hashes_and_source_outbox_commit_together_and_duplicate_is_noop(
    validation_case, monkeypatch
):
    import hashlib

    from app.modules.materials import object_validation

    context, object_id, material_id, _, file_id, dispatch_id = validation_case
    monkeypatch.setattr(
        object_validation,
        "inspect_video",
        lambda *_args, **_kwargs: {"width": 1080, "height": 1920, "duration": 1.0},
    )
    with Session(engine) as session:
        transport = FakeStorage(
            session.get(TemporaryMaterialObject, object_id),
            session.get(MaterialFile, material_id),
        )
        payload = session.get(PendingDispatch, dispatch_id).payload
    for _ in range(2):
        validate_original(
            database_engine=engine,
            context=context,
            object_id=object_id,
            dispatch_id=dispatch_id,
            generation=1,
            revision=payload["revision"],
            s3=transport,
        )
    assert transport.reads == 1
    with Session(engine) as session:
        obj = session.get(TemporaryMaterialObject, object_id)
        assert obj.status == "verified"
        assert obj.sha256 == hashlib.sha256(b"video").hexdigest()
        assert obj.video_md5 == hashlib.md5(b"video", usedforsecurity=False).hexdigest()
        row = session.get(IngestSessionFile, file_id)
        dispatch = session.get(PendingDispatch, row.dispatch_id)
        assert dispatch.task_name == "materials.upload_original"
        assert dispatch.payload == {
            "material_id": str(material_id),
            "object_id": str(object_id),
            "generation": 1,
        }
        assert session.get(MaterialFile, material_id).digest_source == "worker_stream"


def test_revoked_actor_after_get_cannot_publish_trusted_source(
    validation_case, monkeypatch
):
    from app.modules.materials import object_validation
    from app.modules.tenants.models import TenantMembership

    context, object_id, material_id, _, _, dispatch_id = validation_case
    monkeypatch.setattr(
        object_validation,
        "inspect_video",
        lambda *_args, **_kwargs: {"width": 1080, "height": 1920, "duration": 1.0},
    )

    def revoke():
        with Session(engine) as session, session.begin():
            member = session.get(
                TenantMembership, (context.tenant_id, context.actor_id)
            )
            member.active = False

    with Session(engine) as session:
        transport = FakeStorage(
            session.get(TemporaryMaterialObject, object_id),
            session.get(MaterialFile, material_id),
            on_read=revoke,
        )
        payload = session.get(PendingDispatch, dispatch_id).payload
    validate_original(
        database_engine=engine,
        context=context,
        object_id=object_id,
        dispatch_id=dispatch_id,
        generation=1,
        revision=payload["revision"],
        s3=transport,
    )
    with Session(engine) as session:
        obj = session.get(TemporaryMaterialObject, object_id)
        assert obj.status == "stored" and obj.digest_verified_at is None
        assert obj.error_code == "tenant_forbidden"
        assert not session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id,
                PendingDispatch.task_name == "materials.upload_original",
            )
        ).first()


def test_late_validation_receipt_cannot_overwrite_new_owner_and_closes_own_use(
    validation_case, monkeypatch
):
    from app.modules.materials import object_validation

    context, object_id, material_id, _, _, dispatch_id = validation_case
    replacement = uuid4()
    monkeypatch.setattr(
        object_validation,
        "inspect_video",
        lambda *_args, **_kwargs: {"width": 1080, "height": 1920, "duration": 1.0},
    )

    def replace_owner():
        with Session(engine) as session, session.begin():
            obj = session.get(TemporaryMaterialObject, object_id)
            obj.claim_token = replacement

    with Session(engine) as session:
        transport = FakeStorage(
            session.get(TemporaryMaterialObject, object_id),
            session.get(MaterialFile, material_id),
            on_read=replace_owner,
        )
        payload = session.get(PendingDispatch, dispatch_id).payload
    validate_original(
        database_engine=engine,
        context=context,
        object_id=object_id,
        dispatch_id=dispatch_id,
        generation=1,
        revision=payload["revision"],
        s3=transport,
    )
    with Session(engine) as session:
        obj = session.get(TemporaryMaterialObject, object_id)
        assert obj.claim_token == replacement and obj.digest_verified_at is None
        assert not session.exec(
            select(OriginalUse).where(
                OriginalUse.tenant_id == context.tenant_id,
                OriginalUse.status == "active",
            )
        ).first()


def test_streaming_validator_uses_real_media_probe_and_removes_temporary_copy(
    tmp_path, monkeypatch
):
    import hashlib
    import subprocess
    from contextlib import contextmanager
    from tempfile import TemporaryDirectory as RealTemporaryDirectory
    from types import SimpleNamespace

    from app.modules.materials import object_validation

    video = tmp_path / "small.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x240:d=0.2",
            "-an",
            "-c:v",
            "mpeg4",
            str(video),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    content = video.read_bytes()
    obj = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        bc_id="test-bc",
        material_id=uuid4(),
        generation=2,
        expected_bytes=len(content),
        storage_bucket="private-test",
        object_key="owned-test/original",
    )
    file = SimpleNamespace(mime_type="video/mp4")
    folders = []

    @contextmanager
    def tracked(**kwargs):
        with RealTemporaryDirectory(**kwargs) as directory:
            folders.append(directory)
            yield directory

    monkeypatch.setattr(object_validation, "TemporaryDirectory", tracked)
    sha256, md5, media = object_validation._read_original(
        FakeStorage(obj, file, body=content), obj, file
    )
    assert sha256 == hashlib.sha256(content).hexdigest()
    assert md5 == hashlib.md5(content, usedforsecurity=False).hexdigest()
    assert media["width"] == 160 and media["height"] == 240
    assert media["duration"] > 0
    from pathlib import Path

    assert folders and all(not Path(folder).exists() for folder in folders)
