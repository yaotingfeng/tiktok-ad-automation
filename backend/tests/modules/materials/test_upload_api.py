from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.db import engine
from app.core.errors import DomainError, domain_error_handler
from app.core.security import create_access_token
from app.modules.materials.models import MaterialFile
from app.modules.tenants.models import TenantMembership
from tests.modules.materials.test_object_uploads import FakeS3
from tests.modules.materials.test_object_uploads import upload_owner as upload_owner


@pytest.fixture
def api(upload_owner, monkeypatch):
    from app.core.config import settings
    from app.modules.materials import uploads
    from app.modules.materials.router import router

    for name, value in {
        "S3_BUCKET": "private",
        "S3_ACCESS_KEY_ID": "fixture-key",
        "S3_SECRET_ACCESS_KEY": "fixture-secret",
    }.items():
        monkeypatch.setattr(settings, name, value)
    s3 = FakeS3()
    monkeypatch.setattr(uploads, "make_s3", lambda: s3)
    # Task3 supplies the real consumer; this isolated app tests only Task2 routes.
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(router, prefix="/api")
    token = create_access_token(upload_owner.actor_id, timedelta(minutes=5))
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, s3, f"/api/tenants/{upload_owner.tenant_id}/materials"


def body(count=1):
    return {
        "request_id": str(uuid4()),
        "bc_id": "bc-a",
        "files": [
            {"file_name": f"Moon_{i:03}.mp4", "size": 100, "mime_type": "video/mp4"}
            for i in range(count)
        ],
    }


def test_upload_api_returns_safe_identity_and_completes(api):
    client, s3, path = api
    response = client.post(path + "/upload-batches", json=body())
    assert response.status_code == 201
    batch = response.json()
    item = batch["files"][0]
    assert "object_key" not in response.text and "s3_upload_id" not in response.text
    sign = client.post(f"{path}/{item['material_id']}/upload-parts/1/sign")
    assert sign.status_code == 200 and sign.json()["expires_in"] == 900
    completion = client.post(
        f"{path}/{item['material_id']}/complete",
        json={"parts": [{"part_number": 1, "etag": "etag"}]},
    )
    assert completion.status_code == 200 and completion.json()["task_id"]
    progress = client.get(f"{path}/upload-batches/{batch['batch_id']}").json()
    assert (
        progress["status"] == "stored" and progress["files"][0]["received_bytes"] == 100
    )
    assert not progress["files"][0]["can_retry"]
    assert any(name == "complete" for name, _ in s3.calls)


@pytest.mark.parametrize(
    "patch",
    [{"advertiser_id": "chosen"}, {"drama_id": str(uuid4())}, {"object_key": "chosen"}],
)
def test_upload_rejects_user_selected_account_drama_or_path(api, patch):
    client, s3, path = api
    request = {**body(), **patch}
    assert client.post(path + "/upload-batches", json=request).status_code == 422
    assert not s3.calls


def test_viewer_read_can_list_but_cannot_upload(api, upload_owner):
    client, _, path = api
    batch = client.post(path + "/upload-batches", json=body()).json()
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (upload_owner.tenant_id, upload_owner.actor_id)
        ).role = "viewer"
    assert client.get(f"{path}/upload-batches/{batch['batch_id']}").status_code == 200
    assert client.post(path + "/upload-batches", json=body()).status_code == 403


def test_directory_pages_are_complete_literal_and_scope_bound(api, upload_owner):
    client, _, path = api
    client.post(path + "/upload-batches", json=body(105))
    ids, cursor = [], None
    while True:
        response = client.get(
            path,
            params={
                "bc_id": "bc-a",
                "query": "Moon_",
                "limit": 50,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        assert response.status_code == 200
        page = response.json()
        ids.extend(row["material_id"] for row in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
        assert (
            client.get(
                path, params={"bc_id": "bc-a", "query": "different", "cursor": cursor}
            ).status_code
            == 422
        )
    assert len(ids) == len(set(ids)) == 105
    assert (
        client.get(path, params={"bc_id": "bc-a", "query": "Moon%"}).json()["items"]
        == []
    )
    detail = client.get(f"{path}/{ids[0]}", params={"bc_id": "bc-a"})
    assert detail.status_code == 200 and detail.json()["material_id"] == ids[0]
    assert "object_key" not in detail.text
    assert (
        client.get(f"{path}/{ids[0]}/assets", params={"bc_id": "bc-a"}).json()["items"]
        == []
    )
    assert (
        client.get(f"{path}/{ids[0]}/attempts", params={"bc_id": "bc-a"}).json()[
            "items"
        ]
        == []
    )
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(MaterialFile).where(
                        MaterialFile.tenant_id == upload_owner.tenant_id
                    )
                ).all()
            )
            == 105
        )


def test_unknown_completion_http_is_safe_and_not_retryable(api):
    client, s3, path = api
    batch = client.post(path + "/upload-batches", json=body()).json()
    material = batch["files"][0]["material_id"]
    assert (
        client.post(
            f"{path}/{material}/complete",
            json={"parts": [{"part_number": 1, "etag": "etag"}]},
        ).status_code
        == 409
    )
    signed = client.post(f"{path}/{material}/upload-parts/1/sign")
    assert signed.status_code == 200 and signed.headers["cache-control"] == "no-store"
    s3.complete_mode = "absent"
    response = client.post(
        f"{path}/{material}/complete",
        json={"parts": [{"part_number": 1, "etag": "etag"}]},
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "object_result_unknown"
    )
    assert response.json()["retryable"] is False
    assert (
        "storage.invalid" not in response.text and "s3_upload_id" not in response.text
    )
    assert client.post(f"{path}/{material}/retry").status_code == 409
    progress = client.get(f"{path}/upload-batches/{batch['batch_id']}").json()
    assert (
        progress["files"][0]["status"] == "result_unknown"
        and not progress["files"][0]["can_retry"]
    )


@pytest.mark.parametrize(
    "file",
    [
        {"file_name": " ", "size": 1, "mime_type": "video/mp4"},
        {"file_name": "Moon.mp4", "size": True, "mime_type": "video/mp4"},
        {"file_name": "Moon.mp4", "size": 0, "mime_type": "video/mp4"},
        {"file_name": "Moon.mp4", "size": 5 * 1024**4 + 1, "mime_type": "video/mp4"},
        {"file_name": "Moon.txt", "size": 100, "mime_type": "text/plain"},
        {
            "file_name": "Moon.mp4",
            "size": 100,
            "mime_type": "video/mp4",
            "object_key": "external",
        },
    ],
)
def test_invalid_files_fail_before_creating_a_batch(api, file):
    client, s3, path = api
    assert (
        client.post(
            path + "/upload-batches", json={**body(), "files": [file]}
        ).status_code
        == 422
    )
    assert not s3.calls


def test_long_unicode_query_cursor_remains_usable(api):
    client, _, path = api
    request = body()
    request["files"] = [
        {"file_name": "月" * 995 + suffix, "size": 100, "mime_type": "video/mp4"}
        for suffix in ("a.mp4", "b.mp4")
    ]
    assert client.post(path + "/upload-batches", json=request).status_code == 201
    params = {"bc_id": "bc-a", "query": "月" * 990, "limit": 1}
    first = client.get(path, params=params)
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor and len(cursor) <= 8192
    assert client.get(path, params={**params, "cursor": cursor}).status_code == 200


def test_attempt_details_expose_only_allowlisted_error_codes(api, upload_owner):
    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialUploadAttempt,
    )
    from tests.modules.materials.test_tenant_materials import mapping

    client, _, path = api
    material = client.post(path + "/upload-batches", json=body()).json()["files"][0][
        "material_id"
    ]
    from uuid import UUID

    with Session(engine) as session, session.begin():
        file = session.get(MaterialFile, UUID(material))
        asset = mapping(session, upload_owner, file)
        operation = MaterialAssetOperation(
            tenant_id=upload_owner.tenant_id,
            bc_id="bc-a",
            material_id=file.id,
            advertiser_id=asset.advertiser_id,
            path="upload_original",
            status="failed",
            request_digest="a" * 64,
        )
        session.add(operation)
        session.flush()
        for code in ("no_upload_account", "arbitrary-credential-value"):
            session.add(
                MaterialUploadAttempt(
                    tenant_id=upload_owner.tenant_id,
                    bc_id="bc-a",
                    material_id=file.id,
                    advertiser_id=asset.advertiser_id,
                    connection_id=asset.connection_id,
                    operation_id=operation.id,
                    status="blocked",
                    request_digest="a" * 64,
                    remote_response={"error_code": code, "secret": "never-public"},
                )
            )
    response = client.get(f"{path}/{material}/attempts", params={"bc_id": "bc-a"})
    assert response.status_code == 200
    assert {row["error_code"] for row in response.json()["items"]} == {
        "no_upload_account",
        "material_response_unknown",
    }
    assert (
        "never-public" not in response.text
        and "arbitrary-credential-value" not in response.text
    )
