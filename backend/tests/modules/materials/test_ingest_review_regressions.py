from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.materials.ingest_models import (
    IngestSessionFile,
    TemporaryMaterialObject,
    transition_ingest_file,
)
from app.modules.materials.ingest_transport import repair_ingest_transports
from tests.conftest import migrated_database as migrated_database
from tests.modules.materials.test_ingest_api import (
    api as api,
)
from tests.modules.materials.test_ingest_api import (
    identity,
    prepare_file,
    receive,
)
from tests.modules.materials.test_ingest_api import (
    remote as remote,
)
from tests.modules.materials.test_object_uploads import upload_owner as upload_owner


def test_new_generation_read_does_not_inherit_previous_received_bytes(api, remote):
    client, _ = api
    parent, url, row = prepare_file(api)
    row = client.post(url + "/resume", json=identity(row)).json()
    receive(remote, row)
    completed = client.post(url + "/complete", json=identity(row))
    assert completed.status_code == 200
    assert client.get(parent).json()["uploaded_count"] == 1
    # Authoritative cleanup outcome is seeded here; no remote cleanup is invoked.
    with Session(engine) as db, db.begin():
        obj = db.exec(
            select(TemporaryMaterialObject).where(
                TemporaryMaterialObject.material_id == UUID(row["material_id"])
            )
        ).one()
        obj.status = "deleted"
        obj.reservation_released_at = datetime.now(UTC)
        obj.deleted_at = obj.reservation_released_at
        file = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == obj.material_id
            )
        ).one()
        transition_ingest_file(
            db,
            tenant_id=file.tenant_id,
            file_id=file.id,
            expected_revision=file.revision,
            status="failed",
        )
    current = client.get(url).json()
    assert current["can_retry"]
    fresh = client.post(url + "/new-generation", json=identity(current))
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["received_bytes"] == 0
    persisted = client.get(url).json()
    assert persisted["generation"] == 2
    assert persisted["received_bytes"] == 0


def test_repair_keeps_unpublished_current_dispatch_backoff(api, remote):
    client, _ = api
    _, url, row = prepare_file(api)
    row = client.post(url + "/resume", json=identity(row)).json()
    receive(remote, row)
    remote.complete_unknown = True
    assert client.post(url + "/complete", json=identity(row)).status_code == 409
    with Session(engine) as db, db.begin():
        obj = db.exec(
            select(TemporaryMaterialObject).where(
                TemporaryMaterialObject.material_id == UUID(row["material_id"])
            )
        ).one()
        obj.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        object_id = obj.id
    with Session(engine) as db, db.begin():
        assert repair_ingest_transports(db) == 1
        obj = db.get(TemporaryMaterialObject, object_id)
        task = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_key
                == f"ingest-reconcile:{object_id}:{obj.revision}"
            )
        ).one()
        task.available_at = datetime.now(UTC) + timedelta(minutes=10)
        task.attempts = 5
        due, dispatch_id = task.available_at, task.id
        obj.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    with Session(engine) as db, db.begin():
        repair_ingest_transports(db)
        task = db.get(PendingDispatch, dispatch_id)
        assert task.attempts == 5
        assert task.available_at == due


def test_disabled_ingest_allows_only_existing_sent_transport_reconciliation(
    api, remote, monkeypatch
):
    from app.modules.materials import ingest_service

    client, _ = api
    _, create_url, initial = prepare_file(api)
    remote.create_unknown = True
    assert (
        client.post(create_url + "/resume", json=identity(initial)).status_code == 409
    )
    remote.create_unknown = False
    _, complete_url, receiving = prepare_file(api)
    receiving = client.post(complete_url + "/resume", json=identity(receiving)).json()
    receive(remote, receiving)
    remote.complete_unknown = True
    assert (
        client.post(complete_url + "/complete", json=identity(receiving)).status_code
        == 409
    )
    remote.complete_unknown = False
    _, untouched_url, untouched = prepare_file(api)
    _, unsent_url, unsent = prepare_file(api)
    unsent = client.post(unsent_url + "/resume", json=identity(unsent)).json()
    receive(remote, unsent)
    monkeypatch.setattr(ingest_service.settings, "MATERIAL_INGEST_ENABLED", False)
    before = len(remote.calls)
    known = client.get(create_url).json()
    response = client.post(create_url + "/resume", json=identity(known))
    assert response.status_code == 200, response.text
    known = client.get(complete_url).json()
    response = client.post(complete_url + "/complete", json=identity(known))
    assert response.status_code == 200, response.text
    assert response.json()["temporary_storage_status"] == "stored"
    assert [name for name, _ in remote.calls[before:]] == ["list_uploads", "head"]
    before = len(remote.calls)
    assert (
        client.post(untouched_url + "/resume", json=identity(untouched)).status_code
        == 503
    )
    assert (
        client.post(unsent_url + "/complete", json=identity(unsent)).status_code == 503
    )
    assert (
        client.post(
            unsent_url + "/part-urls", json={**identity(unsent), "part_numbers": [1]}
        ).status_code
        == 503
    )
    assert len(remote.calls) == before
