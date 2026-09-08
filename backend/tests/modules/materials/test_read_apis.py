"""HTTP read contracts with real PG and offline botocore signing."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import TenantBC
from app.modules.materials.models import MaterialFile, ObjectUpload, UploadBatch
from app.modules.tenants.models import TenantMembership
from tests.modules.materials.test_object_uploads import upload_owner as upload_owner
from tests.modules.materials.test_upload_api import api as api
from tests.modules.materials.test_upload_api import body


def outbox_count():
    with Session(engine) as session:
        return session.exec(select(func.count()).select_from(PendingDispatch)).one()


def test_lost_create_response_lookup_returns_original_batch_without_writes(
    api, upload_owner
):
    client, s3, path = api
    request = body(2)
    original = client.post(path + "/upload-batches", json=request)
    assert original.status_code == 201
    before = outbox_count()
    with Session(engine) as session, session.begin():
        session.add(TenantBC(tenant_id=upload_owner.tenant_id, bc_id="bc-other"))
        session.get(
            TenantMembership, (upload_owner.tenant_id, upload_owner.actor_id)
        ).role = "viewer"
    recovered = client.get(
        f"{path}/upload-requests/{request['request_id']}", params={"bc_id": "bc-a"}
    )
    assert recovered.status_code == 200 and recovered.json() == original.json()
    assert (
        client.get(
            f"{path}/upload-requests/{request['request_id']}",
            params={"bc_id": "bc-other"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"{path}/upload-requests/{uuid4()}", params={"bc_id": "bc-a"}
        ).status_code
        == 404
    )
    assert not s3.calls and outbox_count() == before
    with Session(engine) as session:
        assert (
            session.exec(
                select(func.count())
                .select_from(UploadBatch)
                .where(UploadBatch.tenant_id == upload_owner.tenant_id)
            ).one()
            == 1
        )


def test_batch_directory_pages_205_batches_with_sql_counts_and_scope_cursor(
    api, upload_owner, monkeypatch
):
    client, s3, path = api
    # Seed actual files, preserving equal-time tie ordering and a foreign BC.
    with Session(engine) as session, session.begin():
        session.add(TenantBC(tenant_id=upload_owner.tenant_id, bc_id="bc-other"))
        expected = []
        when = datetime.now(UTC)
        for index in range(206):
            bc = "bc-a" if index < 205 else "bc-other"
            batch = UploadBatch(
                tenant_id=upload_owner.tenant_id,
                bc_id=bc,
                actor_id=upload_owner.actor_id,
                request_id=uuid4(),
                request_digest="a" * 64,
                created_at=when - timedelta(seconds=index // 3),
                status="receiving",
            )
            session.add(batch)
            session.flush()
            for number in range(1 + index % 3):
                file = MaterialFile(
                    tenant_id=upload_owner.tenant_id,
                    bc_id=bc,
                    file_name=f"{index}-{number}.mp4",
                    object_key=f"offline/{uuid4()}",
                    byte_size=10,
                )
                session.add(file)
                session.flush()
                session.add(
                    ObjectUpload(
                        tenant_id=upload_owner.tenant_id,
                        bc_id=bc,
                        material_id=file.id,
                        batch_id=batch.id,
                        object_key=file.object_key,
                        expected_size=10,
                    )
                )
            if index < 205:
                expected.append((batch.created_at, batch.id, 1 + index % 3))
        session.get(
            TenantMembership, (upload_owner.tenant_id, upload_owner.actor_id)
        ).role = "viewer"
    before = outbox_count()
    monkeypatch.setattr(
        "app.modules.materials.uploads._batch_files",
        lambda *_a, **_kw: pytest.fail("list loaded full batch files"),
    )
    found, cursor, sizes, statements = [], None, [], []

    def observe(_c, _cur, sql, _p, _ctx, _many):
        statements.append(sql)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        while True:
            response = client.get(
                path + "/upload-batches",
                params={
                    "bc_id": "bc-a",
                    "limit": 100,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
            assert response.status_code == 200
            page = response.json()
            found.extend(page["items"])
            sizes.append(len(page["items"]))
            cursor = page["next_cursor"]
            if not cursor:
                break
            assert (
                client.get(
                    path + "/upload-batches",
                    params={"bc_id": "bc-other", "cursor": cursor},
                ).status_code
                == 422
            )
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert sizes == [100, 100, 5]
    expected.sort(reverse=True)
    assert [(row["batch_id"], row["file_count"]) for row in found] == [
        (str(identity), count) for _, identity, count in expected
    ]
    assert all(
        set(row) == {"batch_id", "bc_id", "status", "file_count", "created_at"}
        for row in found
    )
    assert all("files" not in row for row in found)
    assert (
        client.get(
            path + "/upload-batches", params={"bc_id": "bc-a", "limit": 51}
        ).status_code
        == 422
    )
    assert client.get(path + "/upload-batches", params={"bc_id": "bc-a"}).json()[
        "next_cursor"
    ]
    assert any("count(" in sql.lower() and "LIMIT" in sql for sql in statements)
    assert not s3.calls and outbox_count() == before


def mark_stored(client, path):
    batch = client.post(path + "/upload-batches", json=body()).json()
    identity = UUID(batch["files"][0]["material_id"])
    with Session(engine) as session, session.begin():
        file = session.get(MaterialFile, identity)
        file.storage_state = "stored"
    return identity


def test_preview_signs_private_get_with_inline_video_only_and_no_network(
    api, upload_owner, monkeypatch, caplog
):
    client, _, path = api
    identity = mark_stored(client, path)
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "https://offline-s3.invalid")
    monkeypatch.setattr(settings, "S3_REGION", "us-east-1")

    def no_network(*_a, **_kw):
        pytest.fail("presigning must not make a network request")

    monkeypatch.setattr("botocore.httpsession.URLLib3Session.send", no_network)
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (upload_owner.tenant_id, upload_owner.actor_id)
        ).role = "viewer"
    before = outbox_count()
    response = client.get(f"{path}/{identity}/preview", params={"bc_id": "bc-a"})
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )
    assert (
        set(response.json()) == {"url", "expires_in"}
        and response.json()["expires_in"] == 300
    )
    parsed = urlsplit(response.json()["url"])
    params = parse_qs(parsed.query)
    assert (
        unquote(parsed.path)
        == f"/private/tenants/{upload_owner.tenant_id}/materials/{identity}/original"
    )
    assert params["response-content-disposition"] == ["inline"]
    assert params["response-content-type"] == ["video/mp4"]
    assert params["X-Amz-Expires"] == ["300"]
    assert "uploadId" not in params and "fixture-secret" not in response.text
    assert "X-Amz-Signature" not in caplog.text and outbox_count() == before


@pytest.mark.parametrize(
    "change,expected_code",
    [
        ("storage_state", "upload_not_ready"),
        ("mime_type", "invalid_file"),
        ("object_key", "object_identity_unverified"),
    ],
)
def test_preview_rejects_incomplete_unsafe_mime_or_foreign_object_path(
    api, change, expected_code
):
    client, _, path = api
    identity = mark_stored(client, path)
    with Session(engine) as session, session.begin():
        file = session.get(MaterialFile, identity)
        setattr(
            file,
            change,
            {
                "storage_state": "receiving",
                "mime_type": "text/html",
                "object_key": f"tenants/{uuid4()}/materials/{identity}/original",
            }[change],
        )
    response = client.get(f"{path}/{identity}/preview", params={"bc_id": "bc-a"})
    assert (
        response.status_code in {409, 422} and response.json()["code"] == expected_code
    )


def test_preview_enforces_bc_permission_and_safe_configuration_errors(
    api, upload_owner, monkeypatch
):
    client, _, path = api
    identity = mark_stored(client, path)
    with Session(engine) as session, session.begin():
        session.add(TenantBC(tenant_id=upload_owner.tenant_id, bc_id="bc-other"))
    assert (
        client.get(
            f"{path}/{identity}/preview", params={"bc_id": "bc-other"}
        ).status_code
        == 404
    )
    assert (
        client.get(f"{path}/{uuid4()}/preview", params={"bc_id": "bc-a"}).status_code
        == 404
    )
    monkeypatch.setattr(settings, "S3_SECRET_ACCESS_KEY", "")
    response = client.get(f"{path}/{identity}/preview", params={"bc_id": "bc-a"})
    assert (
        response.status_code == 422
        and response.json()["code"] == "object_storage_unconfigured"
    )
    assert "fixture-key" not in response.text
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (upload_owner.tenant_id, upload_owner.actor_id)
        ).active = False
    assert (
        client.get(f"{path}/{identity}/preview", params={"bc_id": "bc-a"}).status_code
        == 403
    )


def test_all_read_routes_keep_tenant_scope_even_for_member_of_both(
    api, upload_owner, monkeypatch
):
    from sqlalchemy import delete
    from sqlmodel import SQLModel

    from app.modules.materials.repository import encode_material_cursor
    from app.modules.tenants.models import Tenant

    client, _, path = api
    request = body()
    original = client.post(path + "/upload-batches", json=request).json()
    identity = UUID(original["files"][0]["material_id"])
    second = uuid4()
    with Session(engine) as session, session.begin():
        session.add(Tenant(id=second, name="offline-second-tenant"))
        session.flush()
        session.add_all(
            [
                TenantMembership(
                    tenant_id=second, user_id=upload_owner.actor_id, role="viewer"
                ),
                TenantBC(tenant_id=second, bc_id="bc-a"),
            ]
        )

    def forbidden(*_a, **_kw):
        pytest.fail("scope rejection attempted object signing")

    monkeypatch.setattr(
        "app.modules.materials.catalog.sign_original_preview", forbidden
    )
    try:
        second_path = f"/api/tenants/{second}/materials"
        before = outbox_count()
        assert (
            client.get(
                f"{second_path}/upload-requests/{request['request_id']}",
                params={"bc_id": "bc-a"},
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"{second_path}/{identity}/preview", params={"bc_id": "bc-a"}
            ).status_code
            == 404
        )
        cursor = encode_material_cursor(
            scope={
                "kind": "upload-batches",
                "tenant": str(upload_owner.tenant_id),
                "bc": "bc-a",
            },
            name=datetime.now(UTC).isoformat(),
            identity=UUID(original["batch_id"]),
        )
        assert (
            client.get(
                second_path + "/upload-batches",
                params={"bc_id": "bc-a", "cursor": cursor},
            ).status_code
            == 422
        )
        empty = client.get(second_path + "/upload-batches", params={"bc_id": "bc-a"})
        assert empty.status_code == 200 and empty.json() == {
            "items": [],
            "next_cursor": None,
        }
        assert outbox_count() == before
    finally:
        with Session(engine) as session, session.begin():
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    session.execute(delete(table).where(table.c.tenant_id == second))
            session.execute(delete(Tenant).where(Tenant.id == second))


def test_signing_exception_is_sanitized_without_url_or_credential_log(
    api, monkeypatch, caplog
):
    from botocore.signers import RequestSigner

    client, _, path = api
    identity = mark_stored(client, path)
    secret = "offline-signing-secret-that-must-not-escape"

    def failure(*_a, **_kw):
        raise ValueError(secret + " https://offline.invalid/?signature=private")

    monkeypatch.setattr(RequestSigner, "generate_presigned_url", failure)
    response = client.get(f"{path}/{identity}/preview", params={"bc_id": "bc-a"})
    assert response.status_code == 503
    assert response.json()["code"] == "object_storage_unavailable"
    assert response.json()["retryable"] is True
    assert secret not in response.text and secret not in caplog.text
