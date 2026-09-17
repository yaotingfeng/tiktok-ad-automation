"""失败原件不能永远占据素材准备槽；替身仅在对象存储边界。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.materials.ingest_models import (
    IngestSessionFile,
    TemporaryMaterialObject,
)
from app.modules.materials.models import MaterialFile
from app.modules.materials.object_validation import (
    repair_validations,
    validate_original,
)
from tests.modules.materials.test_validation_dispatch import FakeStorage
from tests.modules.materials.test_validation_dispatch import (
    validation_case as validation_case,
)


def invoke(case, transport):
    context, object_id, _, _, _, dispatch_id = case
    with Session(engine) as db:
        payload = db.get(PendingDispatch, dispatch_id).payload
    validate_original(
        database_engine=engine,
        context=context,
        object_id=object_id,
        dispatch_id=dispatch_id,
        generation=payload["generation"],
        revision=payload["revision"],
        s3=transport,
    )


def test_corrupt_original_stops_after_one_read_and_old_delivery_is_noop(
    validation_case,
):
    _, object_id, material_id, _, file_id, dispatch_id = validation_case
    with Session(engine) as db, db.begin():
        dispatch = db.get(PendingDispatch, dispatch_id)
        dispatch.attempts, dispatch.published_at = 1, datetime.now(UTC)
    with Session(engine) as db:
        transport = FakeStorage(
            db.get(TemporaryMaterialObject, object_id),
            db.get(MaterialFile, material_id),
            body=b"bad",
        )
    invoke(validation_case, transport)
    with Session(engine) as db:
        row = db.get(IngestSessionFile, file_id)
        assert row.status == "failed" and row.error_code == "incomplete_object"
        assert row.dispatch_id is None
        assert db.get(TemporaryMaterialObject, object_id).status == "stored"
    invoke(validation_case, transport)
    assert transport.reads == 1


@pytest.mark.parametrize("error", ["invalid_video", "incomplete_object"])
def test_historic_permanent_failure_is_not_repaired_or_downloaded(
    validation_case, error
):
    _, object_id, material_id, _, file_id, dispatch_id = validation_case
    with Session(engine) as db, db.begin():
        obj = db.get(TemporaryMaterialObject, object_id)
        obj.error_code = error
        obj.next_attempt_at = datetime.now(UTC) - timedelta(minutes=1)
        row = db.get(IngestSessionFile, file_id)
        row.status, row.error_code = "failed", error
        dispatch = db.get(PendingDispatch, dispatch_id)
        dispatch.attempts = 2400
        dispatch.available_at = dispatch.published_at = obj.next_attempt_at - timedelta(
            minutes=5
        )
    with Session(engine) as db:
        transport = FakeStorage(
            db.get(TemporaryMaterialObject, object_id),
            db.get(MaterialFile, material_id),
        )
    with Session(engine) as db, db.begin():
        assert repair_validations(db) == 0
    invoke(validation_case, transport)
    assert transport.reads == 0
    with Session(engine) as db:
        assert db.get(IngestSessionFile, file_id).dispatch_id is None


@pytest.mark.parametrize("attempts", [1, 3])
def test_transient_storage_failure_has_bounded_delivery_budget(
    validation_case, attempts
):
    _, _, _, _, file_id, dispatch_id = validation_case

    class UnavailableStorage:
        def head_object(self, **kwargs):
            raise TimeoutError("offline storage transport")

    with Session(engine) as db, db.begin():
        dispatch = db.get(PendingDispatch, dispatch_id)
        dispatch.attempts, dispatch.published_at = attempts, datetime.now(UTC)
    invoke(validation_case, UnavailableStorage())
    with Session(engine) as db:
        row = db.get(IngestSessionFile, file_id)
        assert row.status == "failed" and row.error_code == "object_storage_unavailable"
        assert row.dispatch_id == (dispatch_id if attempts == 1 else None)
        dispatch = db.get(PendingDispatch, dispatch_id)
        assert (dispatch.published_at is None) == (attempts == 1)
