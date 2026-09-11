"""Official SDK including serializer/deserializer; only urllib3 transport is doubled."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from urllib3.response import HTTPResponse

from app.integrations.tiktok.contracts.materials import (
    FileVideoUpload,
    RemoteCallBudget,
)
from app.integrations.tiktok.sdk import official_client
from app.modules.materials.sdk_assets import (
    verified_video,
)
from tests.modules.materials.test_url_sdk_contract import adapter


@pytest.fixture
def transport(monkeypatch):
    calls = []
    responses = []

    def request(_pool, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return HTTPResponse(body=json.dumps(responses.pop(0)).encode(), status=200)

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    return calls, responses


def test_official_multipart_and_real_success_envelope(transport, tmp_path):
    calls, responses = transport
    row = {
        "video_id": "returned-vid",
        "material_id": "actual-mid",
        "displayable": False,
    }
    responses.append({"code": 0, "request_id": "request-1", "data": [row]})
    path = tmp_path / "offline.mp4"
    path.write_bytes(b"fixture only")
    with official_client(access_token="test-only-token") as client:
        budget = RemoteCallBudget(
            deadline=datetime.now(UTC) + timedelta(seconds=900),
            hard_limit_seconds=900,
            lease_ms=970000,
        )
        receipt = adapter(client, budget).upload_video_file(
            FileVideoUpload(
                advertiser_id="actual-account",
                local_path=str(path),
                file_name="internal.mp4",
                expected_md5="a" * 32,
                byte_size=12,
            ),
            budget=budget,
        )
    assert (receipt.video_id, receipt.mid) == ("returned-vid", "actual-mid")
    method, url, kwargs = calls[0]
    assert method == "POST" and url.endswith("/file/video/ad/upload/")
    fields = dict(kwargs["fields"])
    assert fields["advertiser_id"] == "actual-account"
    assert fields["video_signature"] == "a" * 32
    assert fields["file_name"] == "internal.mp4"
    assert fields["video_file"][1] == b"fixture only"
    assert kwargs["headers"]["Access-Token"] == "test-only-token"
    assert kwargs["timeout"].read_timeout == 300
    assert "Access-Token" not in client.default_headers


def test_info_and_search_use_actual_target_and_pinned_filter(transport):
    calls, responses = transport
    responses.extend(
        [
            {"code": 0, "data": {"list": []}},
            {
                "code": 0,
                "data": {
                    "list": [],
                    "page_info": {"page": 2, "page_size": 100, "total_page": 2},
                },
            },
        ]
    )
    with official_client(access_token="test-only-token") as client:
        budget = RemoteCallBudget(
            deadline=datetime.now(UTC) + timedelta(seconds=50),
            hard_limit_seconds=50,
            lease_ms=60000,
        )
        facade = adapter(client, budget)
        assert (
            facade.read_video(advertiser_id="target", video_id="vid", budget=budget)
            is None
        )
        facade.search_videos(
            advertiser_id="target", page=2, material_ids=("actual-mid",), budget=budget
        )
    first = dict(calls[0][2]["fields"])
    second = dict(calls[1][2]["fields"])
    assert first["advertiser_id"] == "target"
    assert json.loads(first["video_ids"]) == ["vid"]
    assert json.loads(second["filtering"]) == {"material_ids": ["actual-mid"]}
    assert second["page"] == 2


def test_verified_identity_is_target_readback_with_hash_and_displayable():
    row = {
        "video_id": "different-target-vid",
        "material_id": "target-mid",
        "signature": "a" * 32,
        "displayable": True,
    }
    assert (
        verified_video({"list": [row]}, md5="a" * 32)["video_id"]
        == "different-target-vid"
    )
    for override in (
        {"displayable": False},
        {"displayable": "true"},
        {"signature": "b" * 32},
        {"video_id": "\u3000"},
    ):
        assert verified_video({"list": [{**row, **override}]}, md5="a" * 32) is None
    assert verified_video({"list": []}, md5="a" * 32) is None
