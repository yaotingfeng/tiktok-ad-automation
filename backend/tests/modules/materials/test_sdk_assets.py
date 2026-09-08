"""Official SDK including serializer/deserializer; only urllib3 transport is doubled."""

import json

import pytest
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.sdk import official_client
from app.modules.materials.sdk_assets import (
    parse_upload,
    read_video,
    search_videos,
    upload_video,
    verified_video,
)


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
        response = upload_video(
            client,
            advertiser_id="actual-account",
            local_path=str(path),
            remote_name="internal.mp4",
            md5="a" * 32,
        )
    assert parse_upload(response) == {"video_id": "returned-vid", "mid": "actual-mid"}
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
        assert read_video(client, advertiser_id="target", video_id="vid") == {
            "list": []
        }
        search_videos(
            client, advertiser_id="target", page=2, material_ids=["actual-mid"]
        )
    first = dict(calls[0][2]["fields"])
    second = dict(calls[1][2]["fields"])
    assert first["advertiser_id"] == "target"
    assert json.loads(first["video_ids"]) == ["vid"]
    assert json.loads(second["filtering"]) == {"material_ids": ["actual-mid"]}
    assert second["page"] == 2


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"data": []},
        {"code": 0, "data": []},
        {"data": {}, "request_id": "x"},
        {"data": [{"material_id": "mid"}], "request_id": "x"},
        {"data": [{"video_id": "   "}], "request_id": "x"},
    ],
)
def test_upload_unknown_schema_never_invents_vid(response):
    with pytest.raises(DomainError):
        parse_upload(response)


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


def test_share_generated_body_keeps_source_mid_distinct_from_target_advertiser(
    transport,
):
    from app.modules.materials.sdk_assets import share_video

    calls, responses = transport
    responses.append({"code": 0, "request_id": "offline-share", "data": {}})
    with official_client(access_token="test-only-token") as client:
        assert (
            share_video(
                client,
                source_advertiser_id="source-account",
                source_mid="source-material-id",
                target_advertiser_id="target-account",
            )
            == {}
        )
    method, url, kwargs = calls[0]
    assert method == "POST" and url.endswith("/creative/asset/share/")
    assert json.loads(kwargs["body"]) == {
        "advertiser_id": "source-account",
        "asset_type": "VIDEO",
        "material_ids": ["source-material-id"],
        "shared_advertiser_ids": ["target-account"],
    }
    assert kwargs["timeout"].read_timeout == 30
