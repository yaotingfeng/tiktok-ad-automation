from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.db import engine
from app.core.errors import DomainError, domain_error_handler
from app.core.security import create_access_token
from app.modules.materials.models import MaterialUploadAttempt, ObjectUpload
from app.modules.materials.router import router
from app.modules.tenants.models import TenantMembership
from tests.modules.materials.test_source_uploads import seed_operation
from tests.modules.materials.test_source_uploads import source_env as source_env


@pytest.fixture
def retry_client(source_env):
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + create_access_token(
            source_env["context"].actor_id, timedelta(minutes=5)
        )
        yield client, f"/api/tenants/{source_env['context'].tenant_id}/materials"


def test_unsent_platform_failure_exposes_safe_retry_and_keeps_original(
    source_env, retry_client
):
    old = seed_operation(
        source_env, status="failed", evidence={"error_code": "account_access_denied"}
    )
    with Session(engine) as session, session.begin():
        attempt = session.exec(
            select(MaterialUploadAttempt).where(
                MaterialUploadAttempt.operation_id == old
            )
        ).one()
        attempt.status = "blocked"
    client, path = retry_client
    before = client.get(f"{path}/upload-batches/{source_env['batch_id']}").json()[
        "files"
    ][0]
    assert before["can_retry"] is True
    response = client.post(f"{path}/{source_env['material_id']}/retry")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "stored"
    assert response.json()["task_id"]
    assert response.json()["received_bytes"] == before["received_bytes"]
    assert response.json()["can_retry"] is False
    assert client.post(f"{path}/{source_env['material_id']}/retry").status_code == 409
    with Session(engine) as session:
        attempts = session.exec(
            select(MaterialUploadAttempt).where(
                MaterialUploadAttempt.material_id == source_env["material_id"]
            )
        ).all()
        assert len(attempts) == 2
        assert {attempt.status for attempt in attempts} == {"blocked", "pending"}


@pytest.mark.parametrize(
    "status,evidence",
    [
        ("result_unknown", {}),
        ("verifying", {}),
        ("succeeded", {}),
        ("failed", {"video_id": "received-video"}),
    ],
)
def test_unknown_or_received_platform_upload_cannot_be_retried(
    source_env, retry_client, status, evidence
):
    op_id = seed_operation(source_env, status=status, evidence=evidence)
    with Session(engine) as session, session.begin():
        session.exec(
            select(MaterialUploadAttempt).where(
                MaterialUploadAttempt.operation_id == op_id
            )
        ).one().status = "blocked"
    client, path = retry_client
    row = client.get(f"{path}/upload-batches/{source_env['batch_id']}").json()["files"][
        0
    ]
    assert row["can_retry"] is False
    assert client.post(f"{path}/{source_env['material_id']}/retry").status_code == 409


def test_recovered_upload_account_can_restart_without_reuploading_the_file(
    source_env, retry_client
):
    with Session(engine) as session, session.begin():
        row = session.exec(
            select(ObjectUpload).where(
                ObjectUpload.material_id == source_env["material_id"]
            )
        ).one()
        row.status, row.error_code = "blocked", "no_upload_account"
    client, path = retry_client
    assert (
        client.get(f"{path}/upload-batches/{source_env['batch_id']}").json()["files"][
            0
        ]["can_retry"]
        is True
    )
    assert client.post(f"{path}/{source_env['material_id']}/retry").status_code == 200


def test_viewer_does_not_receive_platform_retry_action(source_env, retry_client):
    with Session(engine) as session, session.begin():
        row = session.exec(
            select(ObjectUpload).where(
                ObjectUpload.material_id == source_env["material_id"]
            )
        ).one()
        row.status, row.error_code = "blocked", "no_upload_account"
        session.get(
            TenantMembership,
            (source_env["context"].tenant_id, source_env["context"].actor_id),
        ).role = "viewer"
    client, path = retry_client
    assert (
        client.get(f"{path}/upload-batches/{source_env['batch_id']}").json()["files"][
            0
        ]["can_retry"]
        is False
    )
    assert client.post(f"{path}/{source_env['material_id']}/retry").status_code == 403
