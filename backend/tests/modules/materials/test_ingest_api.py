"""Authenticated ingest API contracts using dedicated PostgreSQL."""

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.main import api_router
from app.core.errors import DomainError, domain_error_handler
from app.core.security import create_access_token
from tests.modules.materials.test_object_uploads import upload_owner as upload_owner


@pytest.fixture
def api(upload_owner, monkeypatch):
    from types import SimpleNamespace

    from app.core.config import settings
    from app.modules.materials import ingest_service

    configured = SimpleNamespace(
        **{
            **settings.model_dump(),
            "MATERIAL_INGEST_ENABLED": True,
            "MATERIAL_URL_MAX_UPLOAD_BYTES": 256 * 1024**2,
        }
    )
    configured.OBJECT_STORAGE_PROVIDER = "r2"
    configured.S3_ENDPOINT_URL = "https://test.r2.cloudflarestorage.com"
    configured.S3_BUCKET = "ingest-test"
    configured.require_object_storage = lambda: None
    monkeypatch.setattr(ingest_service, "settings", configured)
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(api_router, prefix="/api")
    token = create_access_token(upload_owner.actor_id, timedelta(minutes=30))
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, f"/api/tenants/{upload_owner.tenant_id}/materials"


def session_body(count=1):
    return {
        "request_id": str(uuid4()),
        "bc_id": "bc-a",
        "file_count": count,
        "total_bytes": count * 100,
    }


def test_large_session_is_metadata_only(api):
    client, path = api
    response = client.post(path + "/ingest-sessions", json=session_body(20_000))
    assert response.status_code == 201
    result = response.json()
    assert result["expected_count"] == 20_000
    assert result["accepted_count"] == 0
    assert "files" not in result


def chunk_body(start=0, count=1):
    return {
        "request_id": str(uuid4()),
        "files": [
            {
                "client_index": index,
                "file_name": "same-local-name.mp4",
                "size": 100,
                "mime_type": "video/mp4",
                "last_modified_ms": 1000 + index,
            }
            for index in range(start, start + count)
        ],
    }


def create(api, count=1):
    client, path = api
    response = client.post(path + "/ingest-sessions", json=session_body(count))
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def test_chunk_replays_identity_and_rejects_changed_client_index(api):
    client, path = api
    session_id = create(api, 3)
    url = f"{path}/ingest-sessions/{session_id}"
    body = chunk_body(count=2)
    first = client.post(url + "/chunks", json=body)
    assert first.status_code == 201, first.text
    again = client.post(url + "/chunks", json=body)
    assert again.status_code == 201
    assert first.json() == again.json()
    assert len({item["material_id"] for item in first.json()["items"]}) == 2
    assert all(
        item["temporary_storage_status"] == "waiting_capacity"
        for item in first.json()["items"]
    )
    assert [item["last_modified_ms"] for item in first.json()["items"]] == [1000, 1001]
    changed = {**body, "files": [{**body["files"][0], "last_modified_ms": 9999}]}
    assert client.post(url + "/chunks", json=changed).status_code == 409
    changed["request_id"] = str(uuid4())
    assert client.post(url + "/chunks", json=changed).status_code == 409
    result = client.get(url).json()
    assert result["accepted_count"] == 2
    assert result["reserved_bytes"] == result["stored_bytes"] == 0
    assert client.get(url + "/chunks/" + body["request_id"]).json() == first.json()


def test_session_request_lookup_and_exact_replay(api):
    client, path = api
    body = session_body(2)
    first = client.post(path + "/ingest-sessions", json=body)
    again = client.post(path + "/ingest-sessions", json=body)
    assert first.status_code == again.status_code == 201
    assert first.json()["session_id"] == again.json()["session_id"]
    lookup = client.get(
        path + "/ingest-requests/" + body["request_id"], params={"bc_id": "bc-a"}
    )
    assert lookup.status_code == 200
    assert lookup.json()["session_id"] == first.json()["session_id"]
    assert (
        client.post(
            path + "/ingest-sessions", json={**body, "total_bytes": 123}
        ).status_code
        == 409
    )


@pytest.mark.parametrize("count", [500, 20_000])
def test_bounded_chunks_and_seek_pages_do_not_scale_summary_queries(api, count):
    from sqlalchemy import event

    from app.core.db import engine

    client, path = api
    session_id = create(api, count)
    url = f"{path}/ingest-sessions/{session_id}"
    registered = set()
    first_body = None
    for start in range(0, count, 200):
        body = chunk_body(start, min(200, count - start))
        first_body = first_body or body
        response = client.post(url + "/chunks", json=body)
        assert response.status_code == 201, response.text
        registered.update(item["material_id"] for item in response.json()["items"])
    assert len(registered) == count
    sealed = client.post(url + "/seal").json()
    assert sealed["sealed"] and sealed["issues"] == []
    assert client.post(url + "/chunks", json=first_body).status_code == 201
    assert client.post(url + "/chunks", json=chunk_body()).status_code == 409
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = client.get(url)
        page = client.get(url + "/files", params={"limit": 100})
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result.status_code == page.status_code == 200
    assert result.json()["accepted_count"] == count
    assert "files" not in result.json()
    assert len(result.content) < 1500
    assert len(page.json()["items"]) == 100
    assert len(statements) <= 20
    assert sum("FROM ingest_session_file" in sql for sql in statements) == 1
    assert not any(
        "COUNT(" in value.upper() or "SUM(" in value.upper() for value in statements
    )
    seen, cursor = [], None
    while True:
        response = client.get(
            url + "/files",
            params={"limit": 100, **({"cursor": cursor} if cursor else {})},
        )
        assert response.status_code == 200
        data = response.json()
        seen.extend(item["material_id"] for item in data["items"])
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == count
    assert set(seen) == registered
    assert client.get(url + "/files", params={"limit": 101}).status_code == 422
    assert (
        client.get(url + "/files", params={"status": "available"}).json()["items"] == []
    )
    cursor = page.json()["next_cursor"]
    assert (
        client.get(
            url + "/files", params={"status": "available", "cursor": cursor}
        ).status_code
        == 422
    )


def test_seal_reports_missing_metadata_then_prevents_new_indexes(api):
    client, path = api
    session_id = create(api, 2)
    url = f"{path}/ingest-sessions/{session_id}"
    body = chunk_body()
    assert client.post(url + "/chunks", json=body).status_code == 201
    seal = client.post(url + "/seal")
    assert seal.status_code == 200
    assert not seal.json()["sealed"]
    assert {issue["code"] for issue in seal.json()["issues"]} == {
        "file_count_mismatch",
        "total_bytes_mismatch",
    }
    assert client.post(url + "/chunks", json=chunk_body(1)).status_code == 201
    assert client.post(url + "/seal").json()["sealed"]
    assert client.post(url + "/chunks", json=body).status_code == 201
    assert client.post(url + "/chunks", json=chunk_body(2)).status_code == 409


@pytest.mark.parametrize(
    "changes",
    [
        {"file_count": 20_001},
        {"total_bytes": 0},
        {"file_count": True},
        {"advertiser_id": "chosen"},
    ],
)
def test_parent_input_is_bounded_and_server_selects_source(api, changes):
    client, path = api
    assert (
        client.post(
            path + "/ingest-sessions", json={**session_body(), **changes}
        ).status_code
        == 422
    )


def test_chunk_input_is_bounded_and_received_total_cannot_exceed_manifest(api):
    client, path = api
    session_id = create(api, 200)
    url = f"{path}/ingest-sessions/{session_id}/chunks"
    assert client.post(url, json=chunk_body(count=201)).status_code == 422
    duplicate = chunk_body(count=2)
    duplicate["files"][1]["client_index"] = 0
    assert client.post(url, json=duplicate).status_code == 422
    oversized = chunk_body()
    oversized["files"][0]["size"] = 20_001
    assert client.post(url, json=oversized).status_code == 409
    assert (
        client.get(f"{path}/ingest-sessions/{session_id}").json()["accepted_count"] == 0
    )


class FakeR2:
    """Only storage transport is doubled; budgets, leases and outbox are real."""

    def __init__(self):
        self.calls, self.uploads, self.objects = [], {}, {}
        self.create_unknown = self.complete_unknown = False

    def record(self, operation, values):
        from sqlalchemy import text
        from sqlmodel import Session

        from app.core.db import engine

        self.calls.append((operation, values))
        key = (
            values.get("Key")
            or values.get("Prefix")
            or values.get("Params", {}).get("Key")
        )
        if key:
            material_id = key.split("/materials/")[1].split("/")[0]
            with Session(engine) as check, check.begin():
                # A separate actual connection can lock the object owner during
                # every RPC: the API holds no material/object lock over I/O.
                check.execute(
                    text(
                        "SELECT id FROM material_file WHERE id = :id FOR UPDATE NOWAIT"
                    ),
                    {"id": material_id},
                ).one()

    def create_multipart_upload(self, **values):
        from botocore.exceptions import ReadTimeoutError

        assert "ACL" not in values
        self.record("create", values)
        remote = str(uuid4())
        self.uploads[remote] = {**values, "parts": []}
        if self.create_unknown:
            raise ReadTimeoutError(endpoint_url="https://secret.invalid")
        return {"UploadId": remote}

    def list_multipart_uploads(self, **values):
        self.record("list_uploads", values)
        return {
            "Uploads": [
                {"Key": item["Key"], "UploadId": remote}
                for remote, item in self.uploads.items()
                if item["Key"].startswith(values["Prefix"])
            ],
            "IsTruncated": False,
        }

    def list_parts(self, **values):
        self.record("list_parts", values)
        parts = [
            part
            for part in self.uploads[values["UploadId"]]["parts"]
            if part["PartNumber"] > values.get("PartNumberMarker", 0)
        ]
        selected = parts[: values["MaxParts"]]
        truncated = len(parts) > len(selected)
        return {
            "Parts": selected,
            "IsTruncated": truncated,
            **(
                {"NextPartNumberMarker": selected[-1]["PartNumber"]}
                if truncated
                else {}
            ),
        }

    def generate_presigned_url(self, operation, **values):
        self.record("sign", {"operation": operation, **values})
        return "https://storage.invalid/part?signed=test-only"

    def complete_multipart_upload(self, **values):
        from botocore.exceptions import ReadTimeoutError

        self.record("complete", values)
        initial = self.uploads[values["UploadId"]]
        self.objects[values["Key"]] = {
            "ContentLength": sum(part["Size"] for part in initial["parts"]),
            "ContentType": initial["ContentType"],
            "Metadata": initial["Metadata"],
        }
        if self.complete_unknown:
            raise ReadTimeoutError(endpoint_url="https://secret.invalid")
        return {"ETag": "not-a-content-digest"}

    def head_object(self, **values):
        from botocore.exceptions import ClientError

        self.record("head", values)
        if values["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return self.objects[values["Key"]]

    def close(self):
        pass


@pytest.fixture
def remote(api, monkeypatch, upload_owner):
    from sqlalchemy import update
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials import storage
    from app.modules.materials.ingest_models import ObjectBudget

    assert api[0] is not None
    value = FakeR2()
    monkeypatch.setattr(storage, "make_object_s3", lambda _obj: value)
    try:
        yield value
    finally:
        # Undo only this fixture tenant's committed accounting before the
        # upload_owner fixture removes its rows; never reset unrelated budgets.
        with Session(engine) as cleanup, cleanup.begin():
            own = cleanup.get(ObjectBudget, f"tenant:{upload_owner.tenant_id}")
            if own:
                cleanup.execute(
                    update(ObjectBudget)
                    .where(ObjectBudget.scope_key == "global")
                    .values(
                        reserved_bytes=ObjectBudget.reserved_bytes - own.reserved_bytes,
                        stored_bytes=ObjectBudget.stored_bytes - own.stored_bytes,
                    )
                )


def identity(row):
    return {key: row[key] for key in ("generation", "upload_id", "operation_revision")}


def prepare_file(api, *, size=100):
    client, path = api
    parent = session_body()
    parent["total_bytes"] = size
    created = client.post(path + "/ingest-sessions", json=parent)
    assert created.status_code == 201, created.text
    url = path + "/ingest-sessions/" + created.json()["session_id"]
    body = chunk_body()
    body["files"][0]["size"] = size
    accepted = client.post(url + "/chunks", json=body)
    assert accepted.status_code == 201, accepted.text
    row = accepted.json()["items"][0]
    return url, url + "/files/" + row["material_id"], row


def receive(remote, row):
    remote.uploads[row["upload_id"]]["parts"] = [
        {
            "PartNumber": number,
            "Size": min(
                row["part_size"], row["size"] - (number - 1) * row["part_size"]
            ),
            "ETag": f"etag-{number}",
        }
        for number in range(1, row["part_count"] + 1)
    ]


def test_resume_sign_list_complete_preserves_exact_identity_and_durable_handoff(
    api, remote
):
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.jobs.models import PendingDispatch
    from app.modules.materials.ingest_models import OriginalUse

    client, _ = api
    parent, url, row = prepare_file(api)
    resumed = client.post(url + "/resume", json=identity(row))
    assert resumed.status_code == 200, resumed.text
    row = resumed.json()
    assert row["temporary_storage_status"] == "receiving"
    assert row["upload_id"]
    initial_revision = row["operation_revision"]
    for _ in range(2):
        signed = client.post(
            url + "/part-urls", json={**identity(row), "part_numbers": [1]}
        )
        assert signed.status_code == 200, signed.text
        assert signed.json()["operation_revision"] == initial_revision
        assert signed.headers["cache-control"] == "no-store"
    sign_args = [values for operation, values in remote.calls if operation == "sign"]
    assert all(args["Params"]["ContentLength"] == 100 for args in sign_args)
    receive(remote, row)
    parts = client.get(url + "/parts", params=identity(row))
    assert parts.status_code == 200, parts.text
    assert parts.json()["items"] == [
        {"part_number": 1, "byte_size": 100, "etag": "etag-1"}
    ]
    completed = client.post(url + "/complete", json=identity(row))
    assert completed.status_code == 200, completed.text
    final = completed.json()
    assert final["temporary_storage_status"] == "stored"
    assert final["upload_id"] == row["upload_id"]
    assert final["task_id"] and final["received_bytes"] == 100
    summary = client.get(parent).json()
    assert summary["reserved_bytes"] == summary["stored_bytes"] == 100
    assert summary["uploaded_count"] == 1
    assert client.post(url + "/complete", json=identity(row)).status_code == 200
    assert len([call for call in remote.calls if call[0] == "complete"]) == 1
    with Session(engine) as db:
        from uuid import UUID

        task = db.get(PendingDispatch, UUID(final["task_id"]))
        assert task.task_name == "materials.validate_original"
        assert set(task.payload) == {"object_id", "generation", "revision"}
        assert "signed" not in str(task.payload)
        assert [
            use.status
            for use in db.exec(
                select(OriginalUse).where(
                    OriginalUse.material_id == UUID(row["material_id"])
                )
            ).all()
        ] == ["released"]


def test_creation_timeout_reconciles_same_upload_without_second_create(api, remote):
    client, _ = api
    parent, url, row = prepare_file(api)
    remote.create_unknown = True
    assert client.post(url + "/resume", json=identity(row)).status_code == 409
    current = client.get(url).json()
    assert current["operation_status"] == "result_unknown"
    remote.create_unknown = False
    resumed = client.post(url + "/resume", json=identity(current))
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["upload_id"] in remote.uploads
    assert len([call for call in remote.calls if call[0] == "create"]) == 1
    assert client.get(parent).json()["reserved_bytes"] == 100


def test_completion_timeout_uses_head_and_never_completes_again(api, remote):
    client, _ = api
    parent, url, row = prepare_file(api)
    row = client.post(url + "/resume", json=identity(row)).json()
    receive(remote, row)
    remote.complete_unknown = True
    assert client.post(url + "/complete", json=identity(row)).status_code == 409
    current = client.get(url).json()
    assert current["operation_status"] == "result_unknown"
    final = client.post(url + "/complete", json=identity(current))
    assert final.status_code == 200, final.text
    assert final.json()["temporary_storage_status"] == "stored"
    assert len([call for call in remote.calls if call[0] == "complete"]) == 1
    assert client.get(parent).json()["uploaded_count"] == 1


def test_capacity_waiting_keeps_metadata_and_issues_no_permission(
    api, remote, monkeypatch
):
    from app.core.config import settings

    client, _ = api
    parent, url, row = prepare_file(api)
    monkeypatch.setattr(settings, "MATERIAL_STORAGE_TENANT_BYTES", 99)
    result = client.post(url + "/resume", json=identity(row))
    assert result.status_code == 200
    assert result.json()["temporary_storage_status"] == "waiting_capacity"
    assert remote.calls == []
    assert client.get(parent).json()["accepted_count"] == 1
    assert client.get(parent).json()["reserved_bytes"] == 0


def test_old_upload_and_revision_cannot_sign_and_cancel_only_queues_cleanup(
    api, remote
):
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.materials.ingest_models import ObjectCleanup

    client, _ = api
    parent, url, first = prepare_file(api)
    row = client.post(url + "/resume", json=identity(first)).json()
    for invalid in (
        {**identity(row), "upload_id": "wrong"},
        {**identity(row), "operation_revision": 999},
    ):
        assert (
            client.post(
                url + "/part-urls", json={**invalid, "part_numbers": [1]}
            ).status_code
            == 409
        )
    assert client.post(url + "/new-generation", json=identity(row)).status_code == 409
    cancelled = client.post(url + "/cancel", json=identity(row))
    assert cancelled.status_code == 200, cancelled.text
    assert (
        client.post(
            url + "/part-urls", json={**identity(cancelled.json()), "part_numbers": [1]}
        ).status_code
        == 409
    )
    assert client.get(parent).json()["reserved_bytes"] == 100
    assert all(operation not in {"abort", "delete"} for operation, _ in remote.calls)
    with Session(engine) as db:
        from uuid import UUID

        record = db.exec(
            select(ObjectCleanup).where(
                ObjectCleanup.material_id == UUID(row["material_id"])
            )
        ).one()
        assert record.reason == "user_cancelled" and record.status == "pending"
        assert "source" not in str(record.eligibility_evidence)


def test_expired_completion_claim_is_repaired_with_head_only(api, remote, upload_owner):
    from datetime import UTC, datetime
    from uuid import UUID

    from sqlmodel import Session, select

    from app.core.db import engine
    from app.jobs.models import PendingDispatch
    from app.modules.materials.ingest_models import TemporaryMaterialObject
    from app.modules.materials.ingest_transport import (
        reconcile_ingest_transport,
        repair_ingest_transports,
    )

    client, _ = api
    parent, url, row = prepare_file(api)
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
        obj.error_code = (
            "multipart_completing"  # process died after send fence, before receipt
        )
        obj.claim_token = uuid4()
        obj.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        obj.next_attempt_at = obj.claimed_until
        token = {
            "object_id": obj.id,
            "generation": obj.generation,
            "revision": obj.revision,
        }
    with Session(engine) as db, db.begin():
        assert repair_ingest_transports(db, limit=1) == 1
        task = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_key
                == f"ingest-reconcile:{token['object_id']}:{token['revision']}"
            )
        ).one()
        assert task.payload == {**token, "object_id": str(token["object_id"])}
    calls = len(remote.calls)
    reconcile_ingest_transport(
        database_engine=engine,
        context=upload_owner,
        **{**token, "revision": token["revision"] - 1},
        s3=remote,
    )
    assert len(remote.calls) == calls
    reconcile_ingest_transport(
        database_engine=engine, context=upload_owner, **token, s3=remote
    )
    assert [name for name, _ in remote.calls[calls:]] == ["head"]
    assert client.get(parent).json()["uploaded_count"] == 1
    with Session(engine) as db, db.begin():
        assert repair_ingest_transports(db, limit=1) == 0


def test_completion_pages_and_independent_part_permissions(api, remote, monkeypatch):
    from uuid import UUID, uuid5

    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.materials import ingest_service
    from app.modules.materials.ingest_models import OriginalUse, TemporaryMaterialObject

    # Metadata only: no test allocates a 505MiB byte buffer.
    monkeypatch.setattr(
        ingest_service.settings, "MATERIAL_URL_MAX_UPLOAD_BYTES", 600 * 1024**2
    )
    client, _ = api
    parent, url, row = prepare_file(api, size=101 * 5 * 1024**2)
    with Session(engine) as db, db.begin():
        obj = db.exec(
            select(TemporaryMaterialObject).where(
                TemporaryMaterialObject.material_id == UUID(row["material_id"])
            )
        ).one()
        obj.part_size = 5 * 1024**2
        object_id = obj.id
    row = client.post(url + "/resume", json=identity(row)).json()
    assert row["part_count"] == 101
    for numbers in ([1, 2], [2]):
        signed = client.post(
            url + "/part-urls", json={**identity(row), "part_numbers": numbers}
        )
        assert signed.status_code == 200, signed.text
        assert signed.json()["operation_revision"] == row["operation_revision"]
    with Session(engine) as db:
        uses = db.exec(
            select(OriginalUse).where(
                OriginalUse.material_id == UUID(row["material_id"])
            )
        ).all()
        assert {use.operation_id for use in uses} == {
            uuid5(object_id, "part:1"),
            uuid5(object_id, "part:2"),
        }
        assert all(use.status == "active" for use in uses)
    receive(remote, row)
    first = client.get(url + "/parts", params={**identity(row), "limit": 100})
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 100 and first.json()["next_cursor"]
    second = client.get(
        url + "/parts",
        params={**identity(row), "limit": 100, "cursor": first.json()["next_cursor"]},
    )
    assert len(second.json()["items"]) == 1 and second.json()["next_cursor"] is None
    calls = len(remote.calls)
    progress = client.post(url + "/complete", json=identity(row))
    assert progress.status_code == 200, progress.text
    assert progress.json()["operation_status"] == "completing"
    assert [name for name, _ in remote.calls[calls:]] == ["list_parts"]
    final = client.post(url + "/complete", json=identity(progress.json()))
    assert final.status_code == 200, final.text
    assert final.json()["temporary_storage_status"] == "stored"
    assert client.get(parent).json()["uploaded_count"] == 1


def test_cancel_untouched_file_allows_new_generation_and_rejects_old(api, remote):
    client, _ = api
    parent, url, first = prepare_file(api)
    cancelled = client.post(url + "/cancel", json=identity(first))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["can_retry"] is True
    current = client.post(url + "/new-generation", json=identity(cancelled.json()))
    assert current.status_code == 200, current.text
    assert current.json()["generation"] == 2 and current.json()["upload_id"] is None
    assert remote.calls == []
    assert client.post(url + "/resume", json=identity(first)).status_code == 409
    assert client.get(parent).json()["accepted_count"] == 1


def test_cancel_during_complete_keeps_receipt_without_starting_validator(
    api, remote, monkeypatch
):
    from uuid import UUID

    from sqlmodel import Session, select

    from app.core.db import engine
    from app.jobs.models import PendingDispatch

    client, _ = api
    parent, url, row = prepare_file(api)
    row = client.post(url + "/resume", json=identity(row)).json()
    receive(remote, row)
    original = remote.complete_multipart_upload

    def complete_and_cancel(**values):
        result = original(**values)
        current = client.get(url).json()
        response = client.post(url + "/cancel", json=identity(current))
        assert response.status_code == 200, response.text
        return result

    monkeypatch.setattr(remote, "complete_multipart_upload", complete_and_cancel)
    response = client.post(url + "/complete", json=identity(row))
    assert response.status_code == 200, response.text
    assert response.json()["temporary_storage_status"] == "cleanup_pending"
    assert response.json()["platform_status"] == "blocked"
    assert response.json()["received_bytes"] == row["size"]
    assert client.get(parent).json()["stored_bytes"] == row["size"]
    with Session(engine) as db:
        assert not db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == UUID(parent.split("/")[3])
            )
        ).all()


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_chunks_share_exact_client_index_without_duplicate_counts(
    api, upload_owner, same_request
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import UUID

    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.ingest_schemas import IngestChunkCreate
    from app.modules.materials.ingest_service import register_chunk

    client, path = api
    parent = client.post(path + "/ingest-sessions", json=session_body(2)).json()
    body = chunk_body(count=2)
    bodies = [
        body,
        {**body, "request_id": body["request_id"] if same_request else str(uuid4())},
    ]
    barrier = Barrier(2)

    def submit(value):
        with Session(engine) as db, db.begin():
            barrier.wait(timeout=10)
            return register_chunk(
                db,
                context=upload_owner,
                session_id=UUID(parent["session_id"]),
                body=IngestChunkCreate(**value),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, bodies))
    assert [row.material_id for row in results[0].items] == [
        row.material_id for row in results[1].items
    ]
    result = client.get(path + "/ingest-sessions/" + parent["session_id"]).json()
    assert result["accepted_count"] == 2


def test_viewer_read_only_and_cross_session_cursor_scope(api, remote, upload_owner):
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.tenants.models import TenantMembership

    client, path = api
    first = client.post(path + "/ingest-sessions", json=session_body(2)).json()
    parent = path + "/ingest-sessions/" + first["session_id"]
    rows = client.post(parent + "/chunks", json=chunk_body(count=2)).json()["items"]
    other = client.post(path + "/ingest-sessions", json=session_body(2)).json()
    foreign = path + "/ingest-sessions/" + other["session_id"]
    cursor = client.get(parent + "/files", params={"limit": 1}).json()["next_cursor"]
    assert client.get(foreign + "/files", params={"cursor": cursor}).status_code == 422
    history = client.get(
        path + "/ingest-sessions", params={"bc_id": "bc-a", "limit": 1}
    ).json()
    assert len(history["items"]) == 1 and history["next_cursor"]
    assert client.get(
        path.replace(str(upload_owner.tenant_id), str(uuid4()))
        + "/ingest-sessions/"
        + first["session_id"]
    ).status_code in (403, 404)
    with Session(engine) as db, db.begin():
        member = db.exec(
            select(TenantMembership).where(
                TenantMembership.tenant_id == upload_owner.tenant_id,
                TenantMembership.user_id == upload_owner.actor_id,
            )
        ).one()
        member.role = "viewer"
    assert client.get(parent).status_code == 200
    assert client.get(parent + "/files").status_code == 200
    assert client.post(parent + "/seal").status_code == 403
    assert (
        client.post(
            parent + "/files/" + rows[0]["material_id"] + "/resume",
            json=identity(rows[0]),
        ).status_code
        == 403
    )
    assert remote.calls == []


def test_disabled_or_unconfigured_ingest_preserves_existing_receipts(
    api, remote, monkeypatch
):
    from app.modules.materials import ingest_service

    client, path = api
    parent, url, row = prepare_file(api)
    monkeypatch.setattr(ingest_service.settings, "MATERIAL_INGEST_ENABLED", False)
    result = client.post(path + "/ingest-sessions", json=session_body())
    assert result.status_code == 503 and result.json()["code"] == "ingest_disabled"
    assert client.get(parent).json()["accepted_count"] == 1
    assert client.post(url + "/resume", json=identity(row)).status_code == 503
    monkeypatch.setattr(ingest_service.settings, "MATERIAL_INGEST_ENABLED", True)

    def missing():
        raise DomainError("object_storage_unconfigured", "storage is unconfigured")

    monkeypatch.setattr(ingest_service.settings, "require_object_storage", missing)
    result = client.post(url + "/resume", json=identity(row))
    assert result.status_code == 422
    assert remote.calls == []
    assert client.get(parent).json()["reserved_bytes"] == 0


def test_cancel_during_create_keeps_exact_upload_for_cleanup(api, remote, monkeypatch):
    client, _ = api
    parent, url, row = prepare_file(api)
    original = remote.create_multipart_upload

    def create_and_cancel(**values):
        result = original(**values)
        current = client.get(url).json()
        cancelled = client.post(url + "/cancel", json=identity(current))
        assert cancelled.status_code == 200, cancelled.text
        return result

    monkeypatch.setattr(remote, "create_multipart_upload", create_and_cancel)
    response = client.post(url + "/resume", json=identity(row))
    assert response.status_code == 200, response.text
    assert response.json()["upload_id"] in remote.uploads
    assert response.json()["temporary_storage_status"] == "cleanup_pending"
    assert response.json()["platform_status"] == "blocked"
    assert client.get(parent).json()["reserved_bytes"] == row["size"]
    assert (
        client.post(url + "/resume", json=identity(response.json())).status_code == 200
    )
    assert len([name for name, _ in remote.calls if name == "create"]) == 1


def test_soft_worker_deadline_retains_send_claim_for_recovery(
    api, remote, monkeypatch, upload_owner
):
    from uuid import UUID

    from billiard.exceptions import SoftTimeLimitExceeded
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.materials.ingest_models import TemporaryMaterialObject
    from app.modules.materials.ingest_schemas import IngestIdentity
    from app.modules.materials.ingest_transport import resume_file

    _, url, row = prepare_file(api)

    def interrupted(**_values):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(remote, "create_multipart_upload", interrupted)
    with pytest.raises(SoftTimeLimitExceeded):
        resume_file(
            database_engine=engine,
            context=upload_owner,
            session_id=UUID(url.split("/ingest-sessions/")[1].split("/")[0]),
            material_id=UUID(row["material_id"]),
            identity=IngestIdentity(**identity(row)),
            s3=remote,
        )
    with Session(engine) as db:
        obj = db.exec(
            select(TemporaryMaterialObject).where(
                TemporaryMaterialObject.material_id == UUID(row["material_id"])
            )
        ).one()
        assert obj.claim_token is not None and obj.claimed_until is not None
        assert (
            obj.error_code == "multipart_creating" and obj.reserved_bytes == row["size"]
        )


def test_transport_worker_wrappers_reject_unbounded_execution():
    from app.modules.materials.ingest_tasks import (
        reconcile_ingest_transport_task,
        repair_ingest_transports_task,
    )

    for invoke in (
        lambda: reconcile_ingest_transport_task.run(
            tenant_id=str(uuid4()), actor_id=str(uuid4()), payload={}
        ),
        lambda: repair_ingest_transports_task.run(limit=1),
    ):
        with pytest.raises(DomainError) as caught:
            invoke()
        assert caught.value.code == "material_worker_unbounded"
