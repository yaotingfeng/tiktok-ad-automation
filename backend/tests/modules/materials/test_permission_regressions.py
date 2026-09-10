"""Regression boundaries for signed permission deadlines and cancellation."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from sqlmodel import Session

from app.core.db import engine
from app.modules.materials import storage
from app.modules.materials.cleanup import run_cleanup
from app.modules.materials.ingest_models import OriginalUse, TemporaryMaterialObject
from tests.modules.materials.test_cleanup import cleanup_case as cleanup_case
from tests.modules.materials.test_cleanup_abandoned import MultipartStorage, cancelled
from tests.modules.materials.test_ingest_api import api as api
from tests.modules.materials.test_ingest_api import identity, prepare_file, receive
from tests.modules.materials.test_ingest_api import remote as remote
from tests.modules.materials.test_object_uploads import upload_owner as upload_owner


def test_wire_signature_must_not_outlive_durable_permission(api, remote, monkeypatch):
    client, _ = api
    _, url, row = prepare_file(api)
    reply = client.post(url + "/resume", json=identity(row))
    assert reply.status_code == 200
    assert remote.calls
    row = reply.json()
    real_sign = storage.sign_part
    # Model scheduling / credential construction delay after ledger commit, not
    # clock skew. Real botocore presigning uses the later execution time.
    signing_time = datetime.now(UTC) + timedelta(seconds=120)
    monkeypatch.setattr("botocore.auth.get_current_datetime", lambda: signing_time)
    transport = boto3.client(
        "s3",
        region_name="auto",
        endpoint_url="https://unit.invalid",
        aws_access_key_id="synthetic-only",
        aws_secret_access_key="synthetic-only",
        config=Config(signature_version="s3v4"),
    )

    def delayed_sign(_client, **kwargs):
        assert engine.pool.checkedout() == 0
        return real_sign(transport, **kwargs)

    monkeypatch.setattr(storage, "sign_part", delayed_sign)
    response = client.post(
        url + "/part-urls",
        json={**identity(row), "request_id": str(uuid4()), "part_numbers": [1]},
    )
    if response.status_code == 409:
        assert response.json()["code"] == "part_permission_expired"
        assert "url" not in response.json() and "items" not in response.json()
        return
    assert response.status_code == 200
    item = response.json()["items"][0]
    query = parse_qs(urlsplit(item["url"]).query)
    signed_at = datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=UTC
    )
    actual_deadline = signed_at + timedelta(seconds=int(query["X-Amz-Expires"][0]))
    with Session(engine) as db:
        deadline = db.get(OriginalUse, UUID(item["permission_id"])).expires_at
    assert actual_deadline <= deadline, (
        f"signed deadline exceeds ledger by {(actual_deadline - deadline).total_seconds():.3f}s"
    )


def test_cancelled_unsent_complete_collection_can_reach_exact_abort(cleanup_case):
    cleanup_id = cancelled(cleanup_case)
    with Session(engine) as db, db.begin():
        obj = db.get(TemporaryMaterialObject, cleanup_case[1])
        obj.error_code = "multipart_collecting"
        obj.claim_token = uuid4()
        obj.claimed_until = datetime.now(UTC) - timedelta(seconds=10)
    remote = MultipartStorage()
    run_cleanup(database_engine=engine, cleanup_id=cleanup_id, s3=remote)
    assert remote.aborts == 1, (
        "unsent collection phase must not permanently prevent Abort after claim expires"
    )


def test_cancel_during_part_listing_must_prevent_new_complete_send(
    api, remote, monkeypatch
):
    client, _ = api
    _, url, row = prepare_file(api)
    row = client.post(url + "/resume", json=identity(row)).json()
    receive(remote, row)
    original = remote.list_parts

    def list_then_cancel(**kwargs):
        result = original(**kwargs)
        latest = client.get(url).json()
        cancelled = client.post(url + "/cancel", json=identity(latest))
        assert cancelled.status_code == 200
        return result

    monkeypatch.setattr(remote, "list_parts", list_then_cancel)
    client.post(url + "/complete", json=identity(row))
    assert not any(name == "complete" for name, values in remote.calls), (
        "cancellation must close the unsent Complete fence"
    )
