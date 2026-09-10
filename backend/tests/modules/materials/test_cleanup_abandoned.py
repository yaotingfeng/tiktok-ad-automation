"""Cancellation recovers only exact owned resources with positive evidence."""

from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError
from sqlmodel import Session

from app.core.db import engine
from app.modules.materials.cleanup import run_cleanup
from app.modules.materials.ingest_models import (
    IngestSession,
    ObjectBudget,
    ObjectCleanup,
    OriginalUse,
    TemporaryMaterialObject,
)
from app.modules.materials.models import MaterialAssetOperation
from tests.modules.materials.test_cleanup import (
    FakeCleanupStorage,
)
from tests.modules.materials.test_cleanup import (
    cleanup_case as cleanup_case,
)


def cancelled(case, *, multipart=True):
    context, object_id, operation_id, _, batch_id = case
    with Session(engine) as db, db.begin():
        obj = db.get(TemporaryMaterialObject, object_id)
        operation = db.get(MaterialAssetOperation, operation_id)
        operation.status = "failed"
        operation.remote_response = {
            "object_id": str(object_id),
            "generation": obj.generation,
            "send_armed": False,
        }
        obj.status = "cleanup_pending"
        if multipart:
            for key in ("global", f"tenant:{context.tenant_id}"):
                db.get(ObjectBudget, key).stored_bytes -= obj.expected_bytes
            db.get(IngestSession, batch_id).stored_bytes -= obj.expected_bytes
            obj.received_at, obj.actual_bytes = None, None
            obj.s3_upload_id = "owned-upload"
        row = ObjectCleanup(
            tenant_id=context.tenant_id,
            bc_id=obj.bc_id,
            material_id=obj.material_id,
            generation=obj.generation,
            reason="user_cancelled",
            eligibility_evidence={"generation": obj.generation},
        )
        db.add(row)
        db.flush()
        return row.id


def due(identity):
    with Session(engine) as db, db.begin():
        db.get(ObjectCleanup, identity).next_attempt_at = datetime.now(UTC) - timedelta(
            seconds=1
        )


class MultipartStorage(FakeCleanupStorage):
    def __init__(self, *, lose_abort=False, late_parts=False):
        super().__init__()
        self.exists, self.open, self.aborts = False, True, 0
        self.lose_abort, self.late_parts = lose_abort, late_parts

    def abort_multipart_upload(self, **kwargs):
        assert kwargs["UploadId"] == "owned-upload"
        self.aborts += 1
        self.open = self.late_parts
        if self.lose_abort:
            self.lose_abort = False
            raise TimeoutError("secret-upload-id")
        return {}

    def list_parts(self, **kwargs):
        assert kwargs["UploadId"] == "owned-upload"
        assert kwargs["MaxParts"] <= 100
        if self.open:
            return {
                "Parts": [{"PartNumber": 1, "Size": 5, "ETag": "late"}],
                "IsTruncated": False,
            }
        raise ClientError(
            {
                "Error": {"Code": "NoSuchUpload"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "ListParts",
        )


def test_cancel_unsigned_multipart_aborts_and_releases_once(cleanup_case):
    identity = cancelled(cleanup_case)
    remote = MultipartStorage()
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=remote)
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=remote)
    with Session(engine) as db:
        assert db.get(TemporaryMaterialObject, cleanup_case[1]).reserved_bytes == 0
        row = db.get(ObjectCleanup, identity)
        assert row.abort_confirmed_at and row.head_confirmed_at
        assert row.delete_confirmed_at is None
    assert remote.aborts == 1 and remote.deletes == 0


def test_cancel_completed_unsent_object_uses_delete(cleanup_case):
    identity = cancelled(cleanup_case, multipart=False)
    remote = FakeCleanupStorage()
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=remote)
    assert remote.deletes == 1
    with Session(engine) as db:
        assert db.get(TemporaryMaterialObject, cleanup_case[1]).reserved_bytes == 0


def test_abort_lost_reply_retains_reservation_until_readback(cleanup_case):
    identity = cancelled(cleanup_case)
    remote = MultipartStorage(lose_abort=True)
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=remote)
    with Session(engine) as db:
        assert db.get(TemporaryMaterialObject, cleanup_case[1]).reserved_bytes == 5
    due(identity)
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=remote)
    with Session(engine) as db:
        assert db.get(TemporaryMaterialObject, cleanup_case[1]).reserved_bytes == 0


def test_late_parts_require_another_abort_and_keep_capacity(cleanup_case):
    identity = cancelled(cleanup_case)
    remote = MultipartStorage(late_parts=True)
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=remote)
    with Session(engine) as db:
        assert db.get(TemporaryMaterialObject, cleanup_case[1]).reserved_bytes == 5
    remote.late_parts = False
    due(identity)
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=remote)
    with Session(engine) as db:
        assert db.get(TemporaryMaterialObject, cleanup_case[1]).reserved_bytes == 0


def test_unknown_put_is_not_ended_by_ttl_or_empty_list(cleanup_case):
    identity = cancelled(cleanup_case)
    context, object_id, _, _, _ = cleanup_case
    with Session(engine) as db, db.begin():
        obj = db.get(TemporaryMaterialObject, object_id)
        db.add(
            OriginalUse(
                tenant_id=context.tenant_id,
                bc_id=obj.bc_id,
                material_id=obj.material_id,
                generation=obj.generation,
                actor_id=context.actor_id,
                purpose="part_put",
                expires_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
    remote = MultipartStorage()
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=remote)
    with Session(engine) as db:
        assert db.get(TemporaryMaterialObject, object_id).reserved_bytes == 5


def test_cancel_does_not_delete_unknown_tiktok_read(cleanup_case):
    identity = cancelled(cleanup_case, multipart=False)
    with Session(engine) as db, db.begin():
        op = db.get(MaterialAssetOperation, cleanup_case[2])
        op.status = "result_unknown"
        op.remote_response = {**op.remote_response, "send_armed": True}
    remote = FakeCleanupStorage()
    run_cleanup(database_engine=engine, cleanup_id=identity, s3=remote)
    assert remote.deletes == 0
    with Session(engine) as db:
        assert db.get(TemporaryMaterialObject, cleanup_case[1]).reserved_bytes == 5
