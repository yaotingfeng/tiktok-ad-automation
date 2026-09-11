"""Public upload acceptance freezes once; idempotent replay never chooses today."""

from uuid import UUID, uuid4

from sqlmodel import Session

from app.core.db import engine
from app.modules.accounts.connection_models import BCDefaultRoute
from app.modules.materials.ingest_models import IngestSession
from app.modules.materials.models import UploadBatch
from tests.modules.materials.test_ingest_api import api as api
from tests.modules.materials.test_ingest_api import session_body
from tests.modules.materials.test_object_uploads import upload_owner as upload_owner


def test_ingest_acceptance_persists_route_and_replay_ignores_missing_current_default(
    api, upload_owner
):
    client, path = api
    body = session_body()
    response = client.post(path + "/ingest-sessions", json=body)
    assert response.status_code == 201
    identity = UUID(response.json()["session_id"])
    with Session(engine) as db, db.begin():
        parent = db.get(IngestSession, identity)
        frozen = dict(parent.frozen_route)
        default = db.get(BCDefaultRoute, (upload_owner.tenant_id, "bc-a"))
        assert frozen["connection_id"] == str(default.connection_id)
        db.delete(default)
    assert client.post(path + "/ingest-sessions", json=body).status_code == 201
    with Session(engine) as db:
        assert db.get(IngestSession, identity).frozen_route == frozen
    assert (
        client.post(path + "/ingest-sessions", json=session_body()).status_code == 409
    )


def test_legacy_upload_acceptance_persists_route_and_replay_ignores_missing_default(
    upload_owner,
):
    from app.modules.materials.schemas import UploadFileRequest
    from app.modules.materials.uploads import start_upload_batch

    request = uuid4()
    files = [
        UploadFileRequest(file_name="original.mp4", size=100, mime_type="video/mp4")
    ]
    with Session(engine) as db, db.begin():
        batch = start_upload_batch(
            db, context=upload_owner, bc_id="bc-a", files=files, request_id=request
        )
        identity = batch.batch_id
        frozen = dict(db.get(UploadBatch, identity).frozen_route)
        db.delete(db.get(BCDefaultRoute, (upload_owner.tenant_id, "bc-a")))
    with Session(engine) as db, db.begin():
        result = start_upload_batch(
            db, context=upload_owner, bc_id="bc-a", files=files, request_id=request
        )
        assert result.batch_id == identity
        assert db.get(UploadBatch, identity).frozen_route == frozen
