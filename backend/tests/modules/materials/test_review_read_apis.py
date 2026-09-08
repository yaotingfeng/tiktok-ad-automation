"""Independent review checks for material GET isolation and local capabilities."""

import logging
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlmodel import Session

from app.api.deps import get_db
from app.core.config import settings
from app.core.db import engine
from app.modules.materials.catalog import (
    find_upload_request,
    original_preview,
    upload_batches_page,
)
from app.modules.materials.models import MaterialFile, UploadBatch
from app.modules.tenants.models import TenantMembership
from tests.modules.materials.test_object_uploads import upload_owner as upload_owner
from tests.modules.materials.test_read_apis import mark_stored
from tests.modules.materials.test_upload_api import api as api
from tests.modules.materials.test_upload_api import body


def test_three_get_routes_execute_read_only_without_external_transport(
    api, monkeypatch, caplog
):
    client, _, path = api
    request = body()
    created = client.post(path + "/upload-batches", json=request).json()
    material_id = UUID(created["files"][0]["material_id"])
    with Session(engine) as session, session.begin():
        session.get(MaterialFile, material_id).storage_state = "stored"
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "https://offline-review.invalid")
    monkeypatch.setattr(settings, "S3_REGION", "us-east-1")

    def no_network(*_a, **_kw):
        pytest.fail("GET used an external transport")

    monkeypatch.setattr("botocore.httpsession.URLLib3Session.send", no_network)
    monkeypatch.setattr("urllib3.PoolManager.request", no_network)

    def read_only_db():
        with Session(engine) as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            yield session
            assert not session.new and not session.dirty and not session.deleted

    client.app.dependency_overrides[get_db] = read_only_db
    try:
        with caplog.at_level(logging.DEBUG):
            for suffix in (
                f"/upload-requests/{request['request_id']}",
                "/upload-batches",
                f"/{material_id}/preview",
            ):
                result = client.get(path + suffix, params={"bc_id": "bc-a"})
                assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        parsed = urlsplit(result.json()["url"])
        assert parse_qs(parsed.query)["X-Amz-Expires"] == ["300"]
        assert "fixture-secret" not in result.text
        assert (
            "fixture-secret" not in caplog.text
            and "X-Amz-Signature=" not in caplog.text
        )
    finally:
        client.app.dependency_overrides.pop(get_db, None)


def test_request_lookup_returns_exact_request_when_several_exist(api, upload_owner):
    client, _, path = api
    requests = [body(1), body(2)]
    originals = [
        client.post(path + "/upload-batches", json=request).json()
        for request in requests
    ]
    with Session(engine) as session, session.begin():
        session.execute(text("SET TRANSACTION READ ONLY"))
        results = [
            find_upload_request(
                session,
                context=upload_owner,
                bc_id="bc-a",
                request_id=UUID(request["request_id"]),
            )
            for request in requests
        ]
        assert [str(result.batch_id) for result in results] == [
            row["batch_id"] for row in originals
        ]
        assert [len(result.files) for result in results] == [1, 2]


def test_default_fifty_pagination_handles_exact_timestamp_ties_and_empty_aggregates(
    upload_owner,
):
    when = datetime.now(UTC)
    identities = [uuid4() for _ in range(101)]
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                UploadBatch(
                    id=identity,
                    tenant_id=upload_owner.tenant_id,
                    bc_id="bc-a",
                    actor_id=upload_owner.actor_id,
                    request_id=uuid4(),
                    request_digest="a" * 64,
                    created_at=when,
                    status="receiving",
                )
                for identity in identities
            ]
        )
    cursor, found, sizes = None, [], []
    with Session(engine) as session, session.begin():
        session.execute(text("SET TRANSACTION READ ONLY"))
        while True:
            page = upload_batches_page(
                session, context=upload_owner, bc_id="bc-a", cursor=cursor
            )
            sizes.append(len(page.items))
            found.extend(row.batch_id for row in page.items)
            assert all(row.file_count == 0 for row in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
    assert sizes == [50, 50, 1] and found == sorted(identities, reverse=True)


def test_revocation_denies_every_get_before_signing_or_returning_inventory(
    api, upload_owner, monkeypatch
):
    client, _, path = api
    identity = mark_stored(client, path)
    request = body()
    client.post(path + "/upload-batches", json=request)
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (upload_owner.tenant_id, upload_owner.actor_id)
        ).active = False
    monkeypatch.setattr(
        "app.modules.materials.catalog.sign_original_preview",
        lambda **_kw: pytest.fail("Revoked membership minted an object capability"),
    )
    for suffix in (
        "/upload-batches",
        f"/upload-requests/{request['request_id']}",
        f"/{identity}/preview",
    ):
        assert client.get(path + suffix, params={"bc_id": "bc-a"}).status_code == 403


def test_signed_preview_dto_does_not_expose_capability_in_repr(
    api, upload_owner, monkeypatch
):
    client, _, path = api
    identity = mark_stored(client, path)
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "https://offline-review.invalid")
    with Session(engine) as session, session.begin():
        session.execute(text("SET TRANSACTION READ ONLY"))
        preview = original_preview(
            session, context=upload_owner, bc_id="bc-a", material_id=identity
        )
    assert "X-Amz-Signature=" in preview.url and "X-Amz-Signature=" not in repr(preview)


def test_same_request_uuid_in_two_authorized_tenants_resolves_only_selected_tenant(
    api, upload_owner
):
    from sqlalchemy import delete
    from sqlmodel import SQLModel

    from app.modules.accounts.models import TenantBC
    from app.modules.tenants.models import Tenant

    client, _, path = api
    request = body()
    first = client.post(path + "/upload-batches", json=request).json()
    other_tenant, other_batch = uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(Tenant(id=other_tenant, name="offline-review-second"))
        session.flush()
        session.add_all(
            [
                TenantMembership(
                    tenant_id=other_tenant, user_id=upload_owner.actor_id, role="viewer"
                ),
                TenantBC(tenant_id=other_tenant, bc_id="bc-a"),
            ]
        )
        session.flush()
        session.add(
            UploadBatch(
                id=other_batch,
                tenant_id=other_tenant,
                bc_id="bc-a",
                actor_id=upload_owner.actor_id,
                request_id=UUID(request["request_id"]),
                request_digest="b" * 64,
            )
        )
    try:
        suffix = f"/upload-requests/{request['request_id']}"
        first_read = client.get(path + suffix, params={"bc_id": "bc-a"})
        second_read = client.get(
            f"/api/tenants/{other_tenant}/materials" + suffix, params={"bc_id": "bc-a"}
        )
        assert first_read.status_code == second_read.status_code == 200
        assert first_read.json()["batch_id"] == first["batch_id"]
        assert second_read.json()["batch_id"] == str(other_batch)
        assert second_read.json()["files"] == []
        assert first["files"][0]["material_id"] not in second_read.text
    finally:
        with Session(engine) as session, session.begin():
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    session.execute(
                        delete(table).where(table.c.tenant_id == other_tenant)
                    )
            session.execute(delete(Tenant).where(Tenant.id == other_tenant))
