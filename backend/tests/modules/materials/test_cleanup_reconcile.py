"""Bounded maintenance eligibility and namespace-only orphan observation."""

from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.materials.ingest_models import ObjectCleanup, OriginalUse
from app.modules.materials.models import MaterialAssetOperation, MaterialUploadAttempt
from tests.modules.materials.test_ingest_models import ingest_fixture, original
from tests.modules.materials.test_object_uploads import upload_owner as upload_owner
from tests.modules.materials.test_tenant_materials import mapping


def module():
    assert find_spec("app.modules.materials.cleanup_reconcile") is not None, (
        "bounded reconciliation is not implemented"
    )
    from app.modules.materials import cleanup_reconcile

    return cleanup_reconcile


def candidate(session, context, *, count=1):
    batch, rows, files = ingest_fixture(session, context, count=count)
    old = datetime.now(UTC) - timedelta(days=3)
    objects = []
    for row, file in zip(rows, files, strict=True):
        file.current_object_generation = 1
        row.status = "receiving"
        objects.append(
            original(
                session,
                context,
                file,
                status="receiving",
                reserved_bytes=file.byte_size,
                reserved_at=old,
                created_at=old,
                next_attempt_at=old,
                s3_upload_id="upload-" + str(file.id),
                storage_provider="r2",
                storage_endpoint="https://test.r2.cloudflarestorage.com",
                storage_bucket="isolated",
            )
        )
    session.flush()
    return batch, rows, files, objects


def test_expired_candidate_queues_intent_without_releasing_or_deleting(
    session, context, monkeypatch
):
    service = module()
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    _, rows, _, objects = candidate(session, context)
    result = service.scan_abandoned_objects(
        session, context=context, limit=100, enqueue=True
    )
    assert result["examined"] == 1 and result["queued"] == 1
    obj = objects[0]
    session.refresh(obj)
    assert obj.status == "cleanup_pending" and obj.reserved_bytes == obj.expected_bytes
    assert obj.reservation_released_at is None
    intent = session.exec(
        select(ObjectCleanup).where(ObjectCleanup.material_id == obj.material_id)
    ).one()
    assert intent.reason == "abandoned" and intent.status == "pending"
    task = session.get(PendingDispatch, intent.dispatch_id)
    assert task.task_name == "materials.cleanup_original"
    assert task.payload == {"cleanup_id": str(intent.id), "generation": 1}
    session.refresh(rows[0])
    assert rows[0].status == "cancelled"


def test_progress_timestamp_prevents_old_created_at_false_abandonment(
    session, context, monkeypatch
):
    service = module()
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    _, _, _, objects = candidate(session, context, count=3)
    now = datetime.now(UTC)
    objects[0].received_at = now
    objects[1].digest_verified_at = now
    session.add(
        OriginalUse(
            tenant_id=context.tenant_id,
            bc_id="bc-a",
            material_id=objects[2].material_id,
            generation=1,
            actor_id=context.actor_id,
            purpose="part_put",
            operation_id=uuid4(),
            expires_at=now + timedelta(seconds=900),
            permission_issued_at=now,
        )
    )
    session.flush()
    result = service.scan_abandoned_objects(session, context=context, enqueue=True)
    assert result["queued"] == 0
    assert {item["reason"] for item in result["items"]} == {"recent_progress"}


def test_unknown_source_is_blocked_and_expired_part_use_only_queues_intent(
    session, context, monkeypatch
):
    service = module()
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    _, _, files, objects = candidate(session, context, count=2)
    asset = mapping(session, context, files[0])
    asset.status = "unavailable"
    op = MaterialAssetOperation(
        tenant_id=context.tenant_id,
        bc_id="bc-a",
        material_id=files[0].id,
        advertiser_id=asset.advertiser_id,
        path="upload_original",
        status="result_unknown",
        request_digest="a" * 64,
    )
    session.add(op)
    old = datetime.now(UTC) - timedelta(days=2)
    use = OriginalUse(
        tenant_id=context.tenant_id,
        bc_id="bc-a",
        material_id=files[1].id,
        generation=1,
        actor_id=context.actor_id,
        purpose="part_put",
        operation_id=uuid4(),
        expires_at=old,
        permission_issued_at=old,
        created_at=old,
    )
    session.add(use)
    session.flush()
    result = service.scan_abandoned_objects(session, context=context, enqueue=True)
    assert result["queued"] == 1
    assert any(item["reason"] == "source_result_unknown" for item in result["items"])
    session.refresh(use)
    session.refresh(op)
    assert use.status == "active" and op.status == "result_unknown"
    assert objects[0].reserved_bytes == objects[0].expected_bytes


def test_cursor_advances_beyond_first_100_unsafe_candidates(
    session, context, monkeypatch
):
    service = module()
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    _, _, _, objects = candidate(session, context, count=101)
    ordered = sorted(
        objects, key=lambda value: (value.status, value.next_attempt_at, value.id)
    )
    for obj in ordered[:100]:
        obj.claim_token = uuid4()
        obj.claimed_until = datetime.now(UTC) + timedelta(minutes=10)
    session.flush()
    first = service.scan_abandoned_objects(
        session, context=context, limit=100, enqueue=True
    )
    assert first["examined"] == 100 and first["queued"] == 0 and first["next_cursor"]
    second = service.scan_abandoned_objects(
        session, context=context, limit=100, enqueue=True, cursor=first["next_cursor"]
    )
    assert second["examined"] == 1 and second["queued"] == 1
    assert second["items"][0]["object_id"] == str(ordered[-1].id)


def test_recent_upload_attempt_counts_as_progress(session, context, monkeypatch):
    service = module()
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    _, _, files, _ = candidate(session, context)
    asset = mapping(session, context, files[0])
    asset.status = "unavailable"
    op = MaterialAssetOperation(
        tenant_id=context.tenant_id,
        bc_id="bc-a",
        material_id=files[0].id,
        advertiser_id=asset.advertiser_id,
        path="upload_original",
        status="failed",
        request_digest="b" * 64,
    )
    session.add(op)
    session.flush()
    session.add(
        MaterialUploadAttempt(
            tenant_id=context.tenant_id,
            bc_id="bc-a",
            material_id=files[0].id,
            advertiser_id=asset.advertiser_id,
            connection_id=asset.connection_id,
            operation_id=op.id,
            request_digest="b" * 64,
            status="failed",
        )
    )
    session.flush()
    assert (
        service.scan_abandoned_objects(session, context=context, enqueue=True)["items"][
            0
        ]["reason"]
        == "recent_progress"
    )


def test_orphan_scan_is_readonly_and_only_recognizes_same_namespace(upload_owner):
    from sqlmodel import Session

    from app.core.db import engine

    context = upload_owner
    service = module()
    with Session(engine) as session, session.begin():
        _, _, _, objects = candidate(session, context)
        obj = objects[0]
        key, size, endpoint = obj.object_key, obj.expected_bytes, obj.storage_endpoint

    class Wire:
        def list_objects_v2(self, **values):
            assert engine.pool.checkedout() == 0
            assert values["Prefix"] == f"tenants/{context.tenant_id}/bc/bc-a/materials/"
            assert values["MaxKeys"] == 100 and values["Bucket"] == "isolated"
            return {
                "IsTruncated": False,
                "Contents": [
                    {"Key": key, "Size": size},
                    {"Key": values["Prefix"] + "unknown-user-key", "Size": 1},
                ],
            }

    result = service.scan_r2_orphans(
        database_engine=engine,
        context=context,
        bc_id="bc-a",
        s3=Wire(),
        storage_provider="r2",
        storage_endpoint=endpoint,
        storage_bucket="isolated",
    )
    assert [item["classification"] for item in result["items"]] == [
        "bound",
        "unknown_owner",
    ]
    assert "unknown-user-key" not in str(result)
    with Session(engine) as session:
        assert not session.exec(
            select(ObjectCleanup).where(ObjectCleanup.tenant_id == context.tenant_id)
        ).all()
    mismatch = service.scan_r2_orphans(
        database_engine=engine,
        context=context,
        bc_id="bc-a",
        s3=Wire(),
        storage_provider="r2",
        storage_endpoint="https://other.r2.cloudflarestorage.com",
        storage_bucket="isolated",
    )
    assert mismatch["items"][0]["classification"] == "namespace_mismatch"


def test_disabled_or_readonly_scan_never_creates_intent_or_rewrites_unknown(
    session, context, monkeypatch
):
    service = module()
    _, _, _, objects = candidate(session, context)
    obj = objects[0]
    obj.error_code = "multipart_create_unknown"
    session.flush()
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    result = service.scan_abandoned_objects(session, context=context)
    assert result["items"][0]["reason"] == "transport_result_unknown"
    assert obj.error_code == "multipart_create_unknown"
    assert not session.exec(
        select(ObjectCleanup).where(ObjectCleanup.tenant_id == context.tenant_id)
    ).all()
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", False)
    assert (
        service.scan_abandoned_objects(session, context=context, enqueue=True)[
            "disabled"
        ]
        is True
    )
    assert obj.status == "receiving" and obj.reserved_bytes == obj.expected_bytes


def test_multipart_orphans_require_exact_upload_identity_and_cursor_scope(upload_owner):
    from sqlmodel import Session

    from app.core.db import engine
    from app.core.errors import DomainError

    service = module()
    context = upload_owner
    with Session(engine) as db, db.begin():
        _, _, _, objects = candidate(db, context)
        obj = objects[0]
        key, remote = obj.object_key, obj.s3_upload_id

    class Wire:
        def list_multipart_uploads(self, **values):
            assert values["Prefix"] == f"tenants/{context.tenant_id}/bc/bc-a/materials/"
            assert values["MaxUploads"] == 100
            return {
                "Uploads": [
                    {"Key": key, "UploadId": remote},
                    {"Key": key, "UploadId": "different-upload"},
                ],
                "IsTruncated": True,
                "NextKeyMarker": key,
                "NextUploadIdMarker": "different-upload",
            }

    kwargs = {
        "database_engine": engine,
        "context": context,
        "bc_id": "bc-a",
        "s3": Wire(),
        "kind": "multipart",
        "storage_provider": "r2",
        "storage_endpoint": "https://test.r2.cloudflarestorage.com",
        "storage_bucket": "isolated",
    }
    result = service.scan_r2_orphans(**kwargs)
    assert [item["classification"] for item in result["items"]] == [
        "bound",
        "multipart_identity_mismatch",
    ]
    assert result["next_cursor"]
    with pytest.raises(DomainError):
        service.scan_r2_orphans(
            **{**kwargs, "kind": "objects", "cursor": result["next_cursor"]}
        )


def test_orphan_transport_rejects_out_of_scope_keys_before_any_intent(
    upload_owner, monkeypatch
):
    from app.core.context import TenantContext
    from app.core.db import engine
    from app.core.errors import DomainError

    service = module()
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)

    class Wire:
        calls = 0

        def list_objects_v2(self, **_values):
            self.calls += 1
            return {
                "IsTruncated": False,
                "Contents": [{"Key": "outside-prefix", "Size": 10}],
            }

    wire = Wire()
    kwargs = {
        "database_engine": engine,
        "context": upload_owner,
        "bc_id": "bc-a",
        "s3": wire,
        "storage_provider": "r2",
        "storage_endpoint": "https://test.r2.cloudflarestorage.com",
        "storage_bucket": "isolated",
        "enqueue": True,
    }
    with pytest.raises(DomainError):
        service.scan_r2_orphans(
            **{
                **kwargs,
                "context": TenantContext(
                    tenant_id=uuid4(), actor_id=upload_owner.actor_id, role="operator"
                ),
            }
        )
    assert wire.calls == 0
    with pytest.raises(DomainError):
        service.scan_r2_orphans(**kwargs)
    assert wire.calls == 1


def test_stale_generation_is_reported_and_unmodified(session, context, monkeypatch):
    service = module()
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    _, rows, files, objects = candidate(session, context)
    files[0].current_object_generation = 2
    rows[0].current_generation = 2
    session.flush()
    result = service.scan_abandoned_objects(session, context=context, enqueue=True)
    assert result["queued"] == 0
    assert result["items"][0]["reason"] == "scope_or_generation_unverified"
    assert objects[0].status == "receiving"


def test_repeated_blocked_scans_deduplicate_same_evidence_but_keep_new_progress(
    session, context, monkeypatch
):
    from app.modules.tenants.models import AuditEvent

    service = module()
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    _, _, files, objects = candidate(session, context)
    obj = objects[0]
    obj.error_code = "multipart_create_unknown"
    session.flush()

    def alerts():
        return session.exec(
            select(AuditEvent).where(
                AuditEvent.tenant_id == context.tenant_id,
                AuditEvent.target_id == str(obj.id),
                AuditEvent.action == "materials.abandonment_blocked",
            )
        ).all()

    for _ in range(3):
        result = service.scan_abandoned_objects(session, context=context, enqueue=True)
        assert result["queued"] == 0
        assert result["items"][0]["reason"] == "transport_result_unknown"
    assert len(alerts()) == 1

    # A newer real receipt remains old enough to be abandoned, but is new evidence.
    obj.received_at = obj.reserved_at + timedelta(minutes=1)
    session.flush()
    service.scan_abandoned_objects(session, context=context, enqueue=True)
    service.scan_abandoned_objects(session, context=context, enqueue=True)
    assert len(alerts()) == 2

    obj.error_code = None
    asset = mapping(session, context, files[0])
    asset.status = "unavailable"
    operation = MaterialAssetOperation(
        tenant_id=context.tenant_id,
        bc_id=obj.bc_id,
        material_id=obj.material_id,
        advertiser_id=asset.advertiser_id,
        path="upload_original",
        status="result_unknown",
        request_digest="d" * 64,
    )
    session.add(operation)
    session.flush()
    service.scan_abandoned_objects(session, context=context, enqueue=True)
    service.scan_abandoned_objects(session, context=context, enqueue=True)
    events = alerts()
    assert len(events) == 3
    assert {event.details["reason"] for event in events} == {
        "transport_result_unknown",
        "source_result_unknown",
    }
    assert obj.reserved_bytes == obj.expected_bytes
    assert operation.status == "result_unknown"
