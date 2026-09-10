"""Each signed window preserves its own direct PUT completion evidence."""

from uuid import UUID, uuid4

from sqlmodel import Session

from app.core.db import engine
from app.modules.materials.ingest_models import OriginalUse
from tests.modules.materials.test_ingest_api import api as api
from tests.modules.materials.test_ingest_api import identity, prepare_file, receive
from tests.modules.materials.test_ingest_api import remote as remote
from tests.modules.materials.test_object_uploads import upload_owner as upload_owner


def signed(api):
    client, _ = api
    _, url, row = prepare_file(api)
    result = client.post(url + "/resume", json=identity(row))
    assert result.status_code == 200, result.text
    row = result.json()
    body = {**identity(row), "request_id": str(uuid4()), "part_numbers": [1]}
    response = client.post(url + "/part-urls", json=body)
    assert response.status_code == 200, response.text
    return url, row, body, response.json()["items"][0]


def receipt(part, outcome, etag=None):
    return {
        **{
            key: part[key]
            for key in (
                "part_number",
                "permission_id",
                "permission_nonce",
                "permission_revision",
            )
        },
        "outcome": outcome,
        "etag": etag,
    }


def test_sign_replay_has_same_permission_without_extending_and_different_request_keeps_old(
    api, remote
):
    client, _ = api
    url, row, body, first = signed(api)
    assert remote.calls
    again = client.post(url + "/part-urls", json=body).json()["items"][0]
    assert again["permission_id"] == first["permission_id"]
    assert again["permission_revision"] == first["permission_revision"]
    newer = client.post(url + "/part-urls", json={**body, "request_id": str(uuid4())})
    assert newer.status_code == 200
    assert newer.json()["items"][0]["permission_id"] != first["permission_id"]
    found = client.get(
        url + "/part-permissions",
        params={**identity(row), "request_id": body["request_id"]},
    )
    assert found.status_code == 200, found.text
    assert found.json()["items"][0]["permission_id"] == first["permission_id"]
    assert "url" not in found.json()["items"][0]


def test_direct_completed_receipt_needs_matching_remote_etag(api, remote):
    client, _ = api
    url, row, _, part = signed(api)
    body = {**identity(row), "receipts": [receipt(part, "completed", "etag-1")]}
    assert client.post(url + "/part-receipts", json=body).status_code == 409
    receive(remote, row)
    result = client.post(url + "/part-receipts", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["accepted_permission_ids"] == [part["permission_id"]]
    assert client.post(url + "/part-receipts", json=body).status_code == 200
    with Session(engine) as db:
        use = db.get(OriginalUse, UUID(part["permission_id"]))
        assert use.status == "active"  # still a usable signed capability
        assert use.completion_evidence["outcome"] == "completed"


def test_unknown_receipt_is_sticky_and_cross_nonce_rejected(api, remote):
    client, _ = api
    url, row, _, part = signed(api)
    body = {**identity(row), "receipts": [receipt(part, "unknown")]}
    assert client.post(url + "/part-receipts", json=body).status_code == 200
    receive(remote, row)
    body["receipts"] = [receipt(part, "completed", "etag-1")]
    assert client.post(url + "/part-receipts", json=body).status_code == 409
    body["receipts"] = [{**receipt(part, "unused"), "permission_nonce": str(uuid4())}]
    assert client.post(url + "/part-receipts", json=body).status_code == 409
