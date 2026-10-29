"""手动图片的真实内容验证、稳定顺序与类型隔离。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.materials.ingest_schemas import IngestFileInput
from app.modules.materials.models import MaterialFile
from app.modules.materials.schemas import UploadFileRequest
from tests.modules.materials.test_ingest_api import api as api
from tests.modules.materials.test_ingest_api import create
from tests.modules.materials.test_object_uploads import upload_owner as upload_owner
from tests.modules.materials.test_tenant_materials import material


@pytest.mark.parametrize(
    "fmt,suffix,mime", [("JPEG", "jpg", "image/jpeg"), ("PNG", "png", "image/png")]
)
def test_real_image_decode(tmp_path, fmt, suffix, mime):
    from PIL import Image

    from app.modules.materials.image_validation import inspect_image

    path = tmp_path / f"photo.{suffix}"
    Image.new("RGB", (32, 24), "red").save(path, format=fmt)
    metadata = inspect_image(path, max_pixels=1000)
    assert (metadata.format, metadata.width, metadata.height, metadata.byte_size) == (
        fmt,
        32,
        24,
        path.stat().st_size,
    )
    request = IngestFileInput(
        client_index=0,
        file_name=path.name,
        size=path.stat().st_size,
        mime_type=mime,
        media_type="IMAGE",
    )
    assert request.media_type == "IMAGE"


@pytest.mark.parametrize("kind", ["corrupt", "oversized", "animated", "wrong_format"])
def test_invalid_images_fail_closed(tmp_path, kind):
    from PIL import Image

    from app.modules.materials.image_validation import inspect_image

    path = tmp_path / "image.png"
    if kind == "corrupt":
        path.write_bytes(b"\x89PNG\r\n\x1a\ninvalid")
    elif kind == "animated":
        Image.new("RGB", (20, 20), "red").save(
            path,
            format="PNG",
            save_all=True,
            append_images=[Image.new("RGB", (20, 20), "blue")],
        )
    else:
        Image.new("RGB", (20, 20)).save(
            path, format="GIF" if kind == "wrong_format" else "PNG"
        )
    with pytest.raises(DomainError):
        inspect_image(path, max_pixels=100 if kind == "oversized" else 1000)


def test_image_admission_is_manual_only_and_mime_suffix_strict():
    valid = {"file_name": "photo.png", "size": 100, "mime_type": "image/png"}
    assert (
        IngestFileInput(client_index=0, media_type="IMAGE", **valid).media_type
        == "IMAGE"
    )
    with pytest.raises(ValidationError):
        UploadFileRequest(**valid)
    for patch in [
        {"file_name": "video.mp4"},
        {"mime_type": "image/jpeg"},
        {"media_type": "VIDEO"},
    ]:
        with pytest.raises(ValidationError):
            IngestFileInput(client_index=0, **{**valid, "media_type": "IMAGE", **patch})


def test_image_batch_has_stable_indexes_and_nullable_duration(api):
    client, path = api
    session_id = create(api, 40)
    url = f"{path}/ingest-sessions/{session_id}"
    body = {
        "request_id": str(uuid4()),
        "files": [
            {
                "client_index": index,
                "file_name": f"image-{index}.png",
                "size": 100,
                "mime_type": "image/png",
                "media_type": "IMAGE",
            }
            for index in reversed(range(40))
        ],
    }
    result = client.post(url + "/chunks", json=body)
    assert result.status_code == 201, result.text
    assert [item["client_index"] for item in result.json()["items"]] == list(range(40))
    assert all(item["media_type"] == "IMAGE" for item in result.json()["items"])
    from sqlmodel import Session

    from app.core.db import engine

    with Session(engine) as session:
        row = session.get(MaterialFile, UUID(result.json()["items"][0]["material_id"]))
        assert row.media_type == "IMAGE" and row.duration is None


def test_content_identity_does_not_cross_media_types(session, context):
    from app.modules.materials.content_identity import (
        content_key,
        material_content_key_expression,
    )

    video = material(session, context, "video.mp4")
    picture = material(session, context, "photo.png")
    picture.media_type = "IMAGE"
    picture.mime_type = "image/png"
    for row in (video, picture):
        row.sha256, row.video_md5 = "a" * 64, "b" * 32
        row.digest_verified_at = datetime.now(UTC)
        session.add(row)
    session.flush()
    assert content_key(video).startswith("sha256:")
    assert content_key(video) != content_key(picture)
    assert session.exec(
        select(material_content_key_expression()).where(MaterialFile.id == picture.id)
    ).one() == content_key(picture)


@pytest.fixture
def validation_case():
    from io import BytesIO

    from PIL import Image
    from sqlalchemy import delete
    from sqlmodel import Session, SQLModel

    from app.core.db import engine
    from app.models import User
    from app.modules.materials.ingest_models import (
        IngestSession,
        IngestSessionFile,
        ObjectBudget,
        record_milestone,
    )
    from app.modules.materials.object_budget import mark_object_stored, reserve_object
    from app.modules.materials.object_validation import enqueue_validation
    from app.modules.tenants.models import Tenant
    from tests.modules.conftest import create_context
    from tests.modules.materials.test_ingest_models import original

    buffer = BytesIO()
    Image.new("RGB", (32, 24), "red").save(buffer, format="PNG")
    size = len(buffer.getvalue())
    with Session(engine) as db, db.begin():
        context = create_context(db)
        file = material(db, context, "photo.png")
        file.media_type, file.mime_type, file.byte_size = "IMAGE", "image/png", size
        db.flush()
        batch = IngestSession(
            tenant_id=context.tenant_id,
            bc_id=file.bc_id,
            actor_id=context.actor_id,
            request_id=uuid4(),
            request_digest="a" * 64,
            expected_files=1,
            expected_bytes=size,
        )
        db.add(batch)
        db.flush()
        row = IngestSessionFile(
            tenant_id=context.tenant_id,
            bc_id=file.bc_id,
            session_id=batch.id,
            client_index=0,
            material_id=file.id,
            media_type="IMAGE",
            byte_size=size,
            manifest_digest="a" * 64,
        )
        db.add(row)
        db.flush()
        obj = original(
            db,
            context,
            file,
            storage_provider="r2",
            storage_endpoint="https://test.r2.cloudflarestorage.com",
            storage_bucket="private-test",
        )
        file.current_object_generation = 1
        db.flush()
        assert reserve_object(db, context=context, object_id=obj.id, byte_size=size)
        mark_object_stored(db, context=context, object_id=obj.id, actual_bytes=size)
        record_milestone(
            db,
            tenant_id=context.tenant_id,
            bc_id=file.bc_id,
            session_id=batch.id,
            material_id=file.id,
            milestone="accepted",
        )
        dispatch_id = enqueue_validation(db, context=context, object_id=obj.id)
        ids = context, obj.id, file.id, batch.id, row.id, dispatch_id
    try:
        yield ids
    finally:
        with Session(engine) as db, db.begin():
            budget = db.get(ObjectBudget, f"tenant:{context.tenant_id}")
            global_budget = db.get(ObjectBudget, "global")
            if budget and global_budget:
                global_budget.reserved_bytes -= budget.reserved_bytes
                global_budget.stored_bytes -= budget.stored_bytes
                db.flush()
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    db.exec(delete(table).where(table.c.tenant_id == context.tenant_id))
            db.exec(delete(Tenant).where(Tenant.id == context.tenant_id))
            db.exec(delete(User).where(User.id == context.actor_id))


def image_transport(case, *, body=None, mime="image/png"):
    from io import BytesIO

    from PIL import Image
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.ingest_models import (
        IngestSessionFile,
        TemporaryMaterialObject,
    )
    from tests.modules.materials.test_validation_dispatch import FakeStorage

    if body is None:
        buffer = BytesIO()
        Image.new("RGB", (32, 24), "red").save(buffer, format="PNG")
        body = buffer.getvalue()
    with Session(engine) as db, db.begin():
        obj = db.get(TemporaryMaterialObject, case[1])
        file = db.get(MaterialFile, case[2])
        row = db.get(IngestSessionFile, case[4])
        file.media_type = row.media_type = "IMAGE"
        file.file_name = "photo.png"
        file.mime_type = mime
        body = body.ljust(obj.expected_bytes, b" ")
        db.flush()
        db.expunge(obj)
        db.expunge(file)
        return FakeStorage(obj, file, body=body)


def test_verified_image_handoff_does_not_dispatch_video_or_repair(validation_case):
    from sqlmodel import Session

    from app.core.db import engine
    from app.jobs.models import PendingDispatch
    from app.modules.materials.ingest_models import (
        IngestSessionFile,
        TemporaryMaterialObject,
    )
    from app.modules.materials.object_validation import repair_validations
    from tests.modules.materials.test_validation_retry_bounds import invoke

    transport = image_transport(validation_case)
    invoke(validation_case, transport)
    with Session(engine) as db, db.begin():
        row = db.get(IngestSessionFile, validation_case[4])
        file = db.get(MaterialFile, validation_case[2])
        obj = db.get(TemporaryMaterialObject, validation_case[1])
        assert (file.duration, file.width, file.height) == (None, 32, 24)
        assert obj.status == "verified" and row.status == "stored"
        assert row.dispatch_id is None
        assert repair_validations(db) == 0
        assert not db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == file.tenant_id,
                PendingDispatch.task_name == "materials.upload_original",
            )
        ).all()
    invoke(validation_case, transport)
    assert transport.reads == 1


@pytest.mark.parametrize("kind", ["corrupt", "disguised", "pixel_limit"])
def test_rejected_image_queues_cleanup_without_releasing_unconfirmed_bytes(
    validation_case, kind, monkeypatch
):
    from sqlmodel import Session

    from app.core.config import settings
    from app.core.db import engine
    from app.modules.materials.ingest_models import (
        IngestSessionFile,
        ObjectCleanup,
        TemporaryMaterialObject,
    )
    from tests.modules.materials.test_validation_retry_bounds import invoke

    transport = image_transport(
        validation_case,
        body=b"invalid" if kind == "corrupt" else None,
        mime="image/jpeg" if kind == "disguised" else "image/png",
    )
    if kind == "pixel_limit":
        monkeypatch.setattr(settings, "MATERIAL_IMAGE_MAX_PIXELS", 100)
    invoke(validation_case, transport)
    with Session(engine) as db:
        row = db.get(IngestSessionFile, validation_case[4])
        obj = db.get(TemporaryMaterialObject, validation_case[1])
        assert row.status == "failed"
        assert row.error_code in {
            "invalid_image",
            "image_type_mismatch",
            "image_pixel_limit",
        }
        assert obj.reservation_released_at is None
        cleanup = db.exec(
            select(ObjectCleanup).where(ObjectCleanup.material_id == obj.material_id)
        ).one()
        assert cleanup.status == "pending" and cleanup.dispatch_id is not None
        cleanup_id = cleanup.id
        assert obj.reserved_bytes > 0
    from app.modules.materials.cleanup import run_cleanup
    from tests.modules.materials.test_cleanup import FakeCleanupStorage

    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    remote = FakeCleanupStorage()
    run_cleanup(database_engine=engine, cleanup_id=cleanup_id, s3=remote)
    run_cleanup(database_engine=engine, cleanup_id=cleanup_id, s3=remote)
    assert remote.deletes == 1
    with Session(engine) as db:
        obj = db.get(TemporaryMaterialObject, validation_case[1])
        assert obj.reserved_bytes == 0 and obj.reservation_released_at is not None


def test_image_original_cancel_respects_active_use_after_handoff_removal(
    validation_case,
    monkeypatch,
):
    from datetime import timedelta

    from sqlmodel import Session

    from app.core.config import settings
    from app.core.db import engine
    from app.modules.materials.cleanup import run_cleanup
    from app.modules.materials.cleanup_reconcile import _evaluate
    from app.modules.materials.ingest_models import (
        ObjectCleanup,
        OriginalUse,
        TemporaryMaterialObject,
    )
    from app.modules.materials.ingest_schemas import IngestIdentity
    from app.modules.materials.ingest_transport import cancel_file
    from app.modules.materials.object_uses import (
        acquire_original_use,
        release_original_use,
    )
    from tests.modules.materials.test_cleanup import FakeCleanupStorage
    from tests.modules.materials.test_cleanup_abandoned import due
    from tests.modules.materials.test_validation_retry_bounds import invoke

    context, object_id, material_id, batch_id, _, _ = validation_case
    invoke(validation_case, image_transport(validation_case))
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    with Session(engine) as db, db.begin():
        obj = db.get(TemporaryMaterialObject, object_id)
        obj.reserved_at = obj.received_at = obj.digest_verified_at = datetime.now(
            UTC
        ) - timedelta(days=3)
        use = acquire_original_use(
            db,
            context=context,
            object_id=object_id,
            purpose="ingest",
            operation_id=uuid4(),
            lifetime_seconds=300,
        )
        use_id, nonce = use.id, use.nonce
        for historical_use in db.exec(
            select(OriginalUse).where(OriginalUse.material_id == material_id)
        ).all():
            historical_use.created_at = historical_use.permission_issued_at = (
                datetime.now(UTC) - timedelta(days=3)
            )
        db.flush()
        result = _evaluate(db, object_id=object_id, now=datetime.now(UTC), enqueue=True)
        assert result["reason"] == "original_use_active" and not result["queued"]
        request_identity = IngestIdentity(
            generation=obj.generation,
            upload_id=obj.s3_upload_id,
            operation_revision=obj.revision,
        )
    cancel_file(
        database_engine=engine,
        context=context,
        session_id=batch_id,
        material_id=material_id,
        identity=request_identity,
    )
    with Session(engine) as db:
        cleanup = db.exec(
            select(ObjectCleanup).where(ObjectCleanup.material_id == material_id)
        ).one()
        identity = cleanup.id
    remote = FakeCleanupStorage()
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=remote)
    assert remote.deletes == 0
    with Session(engine) as db, db.begin():
        assert db.get(OriginalUse, use_id).status == "active"
        assert db.get(TemporaryMaterialObject, object_id).reserved_bytes > 0
        release_original_use(db, use_id=use_id, nonce=nonce)
    due(identity)
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=remote)
    assert remote.deletes == 1
    with Session(engine) as db:
        assert db.get(TemporaryMaterialObject, object_id).reserved_bytes == 0


@pytest.mark.parametrize(
    "size,accepted", [(20 * 1024**2, True), (20 * 1024**2 + 1, False)]
)
def test_image_byte_admission_boundary(api, size, accepted):
    client, path = api
    body = {
        "request_id": str(uuid4()),
        "bc_id": "bc-a",
        "file_count": 1,
        "total_bytes": size,
    }
    result = client.post(path + "/ingest-sessions", json=body)
    assert result.status_code == 201
    endpoint = f"{path}/ingest-sessions/{result.json()['session_id']}/chunks"
    response = client.post(
        endpoint,
        json={
            "request_id": str(uuid4()),
            "files": [
                {
                    "client_index": 0,
                    "media_type": "IMAGE",
                    "file_name": "photo.png",
                    "mime_type": "image/png",
                    "size": size,
                }
            ],
        },
    )
    assert response.status_code == (201 if accepted else 422)


def test_image_cannot_enter_video_source_retry(session, context):
    from app.modules.materials.source_uploads import request_source_retry

    row = material(session, context, "photo.png")
    row.media_type, row.mime_type = "IMAGE", "image/png"
    session.flush()
    with pytest.raises(DomainError) as caught:
        request_source_retry(session, context=context, material_id=row.id)
    assert caught.value.code == "image_source_unavailable"


def test_video_manifest_digest_preserves_existing_resume_identity(api):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.ingest_models import IngestSessionFile
    from app.modules.materials.ingest_service import digest
    from tests.modules.materials.test_ingest_api import chunk_body

    client, path = api
    session_id = create(api)
    body = chunk_body()
    result = client.post(f"{path}/ingest-sessions/{session_id}/chunks", json=body)
    assert result.status_code == 201
    with Session(engine) as db:
        row = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.session_id == UUID(session_id)
            )
        ).one()
        assert row.manifest_digest == digest(body["files"][0])


def test_image_pixel_boundary_and_truncated_jpeg(tmp_path):
    from PIL import Image

    from app.modules.materials.image_validation import inspect_image

    path = tmp_path / "boundary.jpg"
    Image.new("RGB", (32, 24), "red").save(path, format="JPEG")
    assert inspect_image(path, max_pixels=768).width == 32
    with pytest.raises(DomainError) as caught:
        inspect_image(path, max_pixels=767)
    assert caught.value.code == "image_pixel_limit"
    path.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(DomainError) as caught:
        inspect_image(path, max_pixels=768)
    assert caught.value.code == "invalid_image"


def test_parallel_completion_preserves_selection_order(api):
    from concurrent.futures import ThreadPoolExecutor

    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.ingest_models import (
        IngestSessionFile,
        transition_ingest_file,
    )

    client, path = api
    session_id = create(api, 4)
    url = f"{path}/ingest-sessions/{session_id}"
    response = client.post(
        url + "/chunks",
        json={
            "request_id": str(uuid4()),
            "files": [
                {
                    "client_index": index,
                    "media_type": "IMAGE",
                    "file_name": f"image-{index}.png",
                    "mime_type": "image/png",
                    "size": 100,
                }
                for index in range(4)
            ],
        },
    )
    assert response.status_code == 201

    def finish(index):
        with Session(engine) as db, db.begin():
            row = db.exec(
                select(IngestSessionFile)
                .where(
                    IngestSessionFile.session_id == UUID(session_id),
                    IngestSessionFile.client_index == index,
                )
                .with_for_update()
            ).one()
            transition_ingest_file(
                db,
                tenant_id=row.tenant_id,
                file_id=row.id,
                expected_revision=row.revision,
                status="stored",
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(finish, [3, 1, 2, 0]))
    page = client.get(url + "/files")
    assert [item["client_index"] for item in page.json()["items"]] == [0, 1, 2, 3]
    assert all(item["platform_status"] == "stored" for item in page.json()["items"])


@pytest.mark.parametrize(
    "code",
    [
        "invalid_image",
        "unsupported_image",
        "image_pixel_limit",
        "image_type_mismatch",
        "image_pipeline_required",
    ],
)
def test_image_failures_have_public_actionable_codes(code):
    from app.modules.materials.uploads import public_error

    assert public_error(code) == code
