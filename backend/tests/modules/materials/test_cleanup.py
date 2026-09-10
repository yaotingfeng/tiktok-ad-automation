"""Cleanup must preserve platform identity and release bytes only after evidence."""

from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import delete
from sqlmodel import Session, SQLModel

from app.core.config import settings
from app.core.db import engine
from app.core.errors import DomainError
from app.models import User
from app.modules.materials.cleanup import run_cleanup, schedule_cleanup
from app.modules.materials.ingest_models import (
    IngestSession,
    ObjectBudget,
    ObjectCleanup,
    OriginalUse,
    TemporaryMaterialObject,
    record_milestone,
)
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialFile,
    MaterialUploadAttempt,
)
from app.modules.materials.object_budget import mark_object_stored, reserve_object
from app.modules.tenants.models import Tenant
from tests.modules.conftest import create_context
from tests.modules.materials.test_ingest_models import original
from tests.modules.materials.test_tenant_materials import mapping
from tests.modules.materials.test_validation_dispatch import ingest_fixture


@pytest.fixture
def cleanup_case(monkeypatch):
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    with Session(engine) as session:
        context = create_context(session)
        batch, _, files = ingest_fixture(session, context)
        file = files[0]
        obj = original(
            session,
            context,
            file,
            storage_provider="r2",
            storage_endpoint="https://test.r2.cloudflarestorage.com",
            storage_bucket="private-test",
        )
        file.current_object_generation = obj.generation
        session.flush()
        assert reserve_object(
            session, context=context, object_id=obj.id, byte_size=obj.expected_bytes
        )
        mark_object_stored(
            session, context=context, object_id=obj.id, actual_bytes=obj.expected_bytes
        )
        obj.status, obj.sha256, obj.video_md5 = "verified", "a" * 64, "b" * 32
        obj.digest_verified_at = datetime.now(UTC)
        file.sha256, file.video_md5, file.digest_verified_at = (
            obj.sha256,
            obj.video_md5,
            obj.digest_verified_at,
        )
        asset = mapping(session, context, file)
        operation = MaterialAssetOperation(
            tenant_id=context.tenant_id,
            bc_id=file.bc_id,
            material_id=file.id,
            advertiser_id=asset.advertiser_id,
            path="upload_original",
            status="succeeded",
            request_digest="a" * 64,
            remote_response={
                "object_id": str(obj.id),
                "generation": obj.generation,
                "video_id": asset.video_id,
                "connection_id": str(asset.connection_id),
            },
        )
        session.add(operation)
        session.flush()
        session.add(
            MaterialUploadAttempt(
                tenant_id=context.tenant_id,
                bc_id=file.bc_id,
                material_id=file.id,
                advertiser_id=asset.advertiser_id,
                connection_id=asset.connection_id,
                operation_id=operation.id,
                request_digest="a" * 64,
                status="available",
            )
        )
        for milestone in ("uploaded", "ready"):
            record_milestone(
                session,
                tenant_id=context.tenant_id,
                bc_id=file.bc_id,
                session_id=batch.id,
                material_id=file.id,
                milestone=milestone,
            )
        session.flush()
        identities = context, obj.id, operation.id, asset.id, batch.id
        session.commit()
    try:
        yield identities
    finally:
        with Session(engine) as session, session.begin():
            budget = session.get(ObjectBudget, f"tenant:{context.tenant_id}")
            global_budget = session.get(ObjectBudget, "global")
            if budget and global_budget:
                global_budget.reserved_bytes -= budget.reserved_bytes
                global_budget.stored_bytes -= budget.stored_bytes
                session.flush()
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    session.exec(
                        delete(table).where(table.c.tenant_id == context.tenant_id)
                    )
            session.exec(delete(Tenant).where(Tenant.id == context.tenant_id))
            session.exec(delete(User).where(User.id == context.actor_id))


class FakeCleanupStorage:
    def __init__(self, *, lose_delete=False, forbidden=False):
        self.exists = True
        self.deletes = 0
        self.heads = 0
        self.lose_delete = lose_delete
        self.forbidden = forbidden

    def delete_object(self, **kwargs):
        assert kwargs["Bucket"] == "private-test"
        self.deletes += 1
        self.exists = False
        if self.lose_delete:
            raise TimeoutError("signed-url-must-not-escape")
        return {}

    def head_object(self, **kwargs):
        self.heads += 1
        if self.forbidden:
            raise ClientError(
                {
                    "Error": {"Code": "AccessDenied"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "HeadObject",
            )
        if not self.exists:
            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        return {"ContentLength": 5}


def scheduled(case):
    _, object_id, operation_id, _, _ = case
    with Session(engine) as session, session.begin():
        first = schedule_cleanup(
            session, object_id=object_id, source_receipt_id=operation_id
        )
        assert (
            schedule_cleanup(
                session, object_id=object_id, source_receipt_id=operation_id
            )
            == first
        )
        return first


def test_verified_source_delete_releases_bytes_once_and_keeps_platform_asset(
    cleanup_case,
):
    context, object_id, _, asset_id, batch_id = cleanup_case
    identity = scheduled(cleanup_case)
    transport = FakeCleanupStorage()
    for _ in range(2):
        run_cleanup(database_engine=engine, cleanup_id=identity, s3=transport)
    assert transport.deletes == 1
    with Session(engine) as session:
        obj = session.get(TemporaryMaterialObject, object_id)
        assert obj.status == "deleted" and obj.reserved_bytes == 0
        assert session.get(AccountMaterial, asset_id).status == "available"
        batch = session.get(IngestSession, batch_id)
        assert (
            batch.ready_count,
            batch.cleaned_count,
            batch.reserved_bytes,
            batch.stored_bytes,
        ) == (1, 1, 0, 0)
        assert session.get(MaterialFile, obj.material_id).file_name == "Moon-video.mp4"


def test_unknown_source_never_schedules_delete_even_after_expiry(cleanup_case):
    _, object_id, operation_id, _, _ = cleanup_case
    with Session(engine) as session, session.begin():
        operation = session.get(MaterialAssetOperation, operation_id)
        operation.status = "result_unknown"
        operation.claimed_until = datetime.now(UTC) - timedelta(days=2)
        session.flush()
        with pytest.raises(DomainError):
            schedule_cleanup(
                session, object_id=object_id, source_receipt_id=operation_id
            )
        assert session.get(TemporaryMaterialObject, object_id).reserved_bytes > 0


def test_active_remote_use_blocks_delete_without_expiring_at_url_ttl(cleanup_case):
    context, object_id, operation_id, _, _ = cleanup_case
    with Session(engine) as session, session.begin():
        obj = session.get(TemporaryMaterialObject, object_id)
        session.add(
            OriginalUse(
                tenant_id=context.tenant_id,
                bc_id=obj.bc_id,
                material_id=obj.material_id,
                generation=obj.generation,
                actor_id=context.actor_id,
                purpose="ingest",
                operation_id=operation_id,
                expires_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
    identity = scheduled(cleanup_case)
    transport = FakeCleanupStorage()
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=transport)
    assert transport.deletes == 0
    with Session(engine) as session:
        assert session.get(TemporaryMaterialObject, object_id).reserved_bytes > 0


def test_lost_delete_reply_is_read_back_even_when_new_deletions_disabled(
    cleanup_case, monkeypatch
):
    _, object_id, _, _, _ = cleanup_case
    identity = scheduled(cleanup_case)
    transport = FakeCleanupStorage(lose_delete=True)
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=transport)
    with Session(engine) as session, session.begin():
        row = session.get(ObjectCleanup, identity)
        assert row.status == "delete_unknown"
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        assert session.get(TemporaryMaterialObject, object_id).reserved_bytes > 0
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", False)
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=transport)
    assert transport.deletes == 1
    with Session(engine) as session:
        assert session.get(TemporaryMaterialObject, object_id).reserved_bytes == 0


def test_head_403_is_not_absence(cleanup_case):
    _, object_id, _, _, _ = cleanup_case
    identity = scheduled(cleanup_case)
    transport = FakeCleanupStorage(forbidden=True)
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=transport)
    with Session(engine) as session:
        assert session.get(TemporaryMaterialObject, object_id).reserved_bytes > 0
        assert session.get(ObjectCleanup, identity).status == "delete_unknown"


def test_already_sent_cleanup_readback_does_not_depend_on_later_asset_status(
    cleanup_case,
):
    _, object_id, _, asset_id, _ = cleanup_case
    identity = scheduled(cleanup_case)
    transport = FakeCleanupStorage(lose_delete=True)
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=transport)
    with Session(engine) as session, session.begin():
        asset = session.get(AccountMaterial, asset_id)
        asset.status = "unavailable"
        cleanup = session.get(ObjectCleanup, identity)
        cleanup.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=transport)
    with Session(engine) as session:
        assert session.get(TemporaryMaterialObject, object_id).reserved_bytes == 0


def test_cleanup_continues_after_original_actor_is_disabled(cleanup_case):
    context, object_id, _, asset_id, _ = cleanup_case
    identity = scheduled(cleanup_case)
    with Session(engine) as session, session.begin():
        actor = session.get(User, context.actor_id)
        actor.is_active = False
    transport = FakeCleanupStorage()
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=transport)
    assert transport.deletes == 1
    with Session(engine) as session:
        assert session.get(TemporaryMaterialObject, object_id).reserved_bytes == 0
        assert session.get(AccountMaterial, asset_id).status == "available"


def test_concurrent_cleanup_workers_send_only_one_delete(cleanup_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    identity = scheduled(cleanup_case)
    entered, release = Event(), Event()

    class BlockingStorage(FakeCleanupStorage):
        def delete_object(self, **kwargs):
            entered.set()
            assert release.wait(timeout=10)
            return super().delete_object(**kwargs)

    transport = BlockingStorage()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            run_cleanup, database_engine=engine, cleanup_id=identity, s3=transport
        )
        try:
            assert entered.wait(timeout=10)
            second = pool.submit(
                run_cleanup, database_engine=engine, cleanup_id=identity, s3=transport
            )
            second.result(timeout=5)
        finally:
            release.set()
        first.result(timeout=5)
    assert transport.deletes == 1


def test_signing_cannot_race_into_an_object_claimed_for_deletion(cleanup_case):
    from app.modules.materials.object_uses import acquire_original_use

    context, object_id, _, _, _ = cleanup_case
    identity = scheduled(cleanup_case)

    class CheckingStorage(FakeCleanupStorage):
        def delete_object(self, **kwargs):
            with Session(engine) as session, session.begin():
                with pytest.raises(DomainError):
                    acquire_original_use(
                        session,
                        context=context,
                        object_id=object_id,
                        purpose="preview",
                        lifetime_seconds=300,
                    )
            return super().delete_object(**kwargs)

    transport = CheckingStorage()
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=transport)
    assert transport.deletes == 1
