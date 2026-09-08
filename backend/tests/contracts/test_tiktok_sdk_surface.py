"""Offline SDK dispatch contracts; these do not establish live Minis support.

Run with ``pytest --confcutdir=tests/contracts tests/contracts -q`` to avoid
the template's database-backed parent conftest.
"""

import socket
from unittest.mock import Mock

import business_api_client as sdk
import pytest

SMART_PLUS_METHODS = [
    (
        sdk.CampaignCreationApi,
        "smart_plus_campaign_create",
        "/open_api/v1.3/smart_plus/campaign/create/",
    ),
    (
        sdk.AdgroupApi,
        "smart_plus_adgroup_create",
        "/open_api/v1.3/smart_plus/adgroup/create/",
    ),
    (sdk.AdApi, "smart_plus_ad_create", "/open_api/v1.3/smart_plus/ad/create/"),
]
SHARE_METHOD = (
    sdk.CreativeManagementApi,
    "creative_asset_share",
    "/open_api/v1.3/creative/asset/share/",
)
UPLOAD_METHOD = (
    sdk.FileApi,
    "ad_video_upload",
    "/open_api/v1.3/file/video/ad/upload/",
)
ALL_METHODS = [*SMART_PLUS_METHODS, SHARE_METHOD, UPLOAD_METHOD]


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def fail_network(*_args, **_kwargs):
        pytest.fail("SDK contracts must not open network connections")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)
    monkeypatch.setattr(socket, "getaddrinfo", fail_network)


@pytest.fixture
def intercepted_client(monkeypatch):
    client = sdk.ApiClient()
    call_api = Mock(return_value={"code": 0, "data": {"offline": True}})
    monkeypatch.setattr(client, "call_api", call_api)
    return client, call_api


@pytest.mark.parametrize("api_class,method_name,path", SMART_PLUS_METHODS)
def test_smart_plus_method_accepts_body_without_network(
    intercepted_client, api_class, method_name, path
):
    client, call_api = intercepted_client
    body = {"advertiser_id": "account-test", "operation_status": "ENABLE"}

    result = getattr(api_class(client), method_name)("test-token", body=body)

    call_api.assert_called_once()
    args, kwargs = call_api.call_args
    assert args[:2] == (path, "POST")
    assert args[4]["Access-Token"] == "test-token"
    assert args[4]["Content-Type"] == "application/json"
    assert kwargs["body"] == body
    assert client.sanitize_for_serialization(kwargs["body"]) == body
    assert result is call_api.return_value


def test_asset_share_preserves_source_and_destination_accounts(intercepted_client):
    client, call_api = intercepted_client
    body = {
        "advertiser_id": "source-account-test",
        "asset_type": "VIDEO",
        "material_ids": ["material-test"],
        "shared_advertiser_ids": ["destination-account-test"],
    }

    sdk.CreativeManagementApi(client).creative_asset_share("test-token", body=body)

    call_api.assert_called_once()
    args, kwargs = call_api.call_args
    assert args[:2] == (SHARE_METHOD[2], "POST")
    assert args[4]["Access-Token"] == "test-token"
    assert args[4]["Content-Type"] == "application/json"
    assert kwargs["body"] == body
    assert client.sanitize_for_serialization(kwargs["body"]) == body


def test_video_upload_dispatches_multipart_file_and_recorded_account(
    intercepted_client, tmp_path
):
    client, call_api = intercepted_client
    video_path = tmp_path / "offline-fixture.mp4"
    video_path.write_bytes(b"offline fixture; not a playable video")

    sdk.FileApi(client).ad_video_upload(
        "test-token",
        advertiser_id="source-account-test",
        upload_type="UPLOAD_BY_FILE",
        video_file=str(video_path),
        file_name=video_path.name,
        auto_bind_enabled=False,
    )

    call_api.assert_called_once()
    args, kwargs = call_api.call_args
    assert args[:2] == (UPLOAD_METHOD[2], "POST")
    assert args[4]["Access-Token"] == "test-token"
    assert args[4]["Content-Type"] == "multipart/form-data"
    assert kwargs["body"] is None
    assert dict(kwargs["post_params"]) == {
        "advertiser_id": "source-account-test",
        "upload_type": "UPLOAD_BY_FILE",
        "file_name": video_path.name,
        "auto_bind_enabled": False,
    }
    assert kwargs["files"] == {"video_file": str(video_path)}


@pytest.mark.parametrize("api_class,method_name,_path", ALL_METHODS)
def test_missing_access_token_is_rejected_before_dispatch(
    intercepted_client, api_class, method_name, _path
):
    client, call_api = intercepted_client

    with pytest.raises(
        ValueError, match="Missing the required parameter `access_token`"
    ):
        getattr(api_class(client), method_name)(None)

    call_api.assert_not_called()


def test_upload_rejects_json_body_before_dispatch(intercepted_client):
    client, call_api = intercepted_client

    with pytest.raises(TypeError, match="unexpected keyword argument 'body'"):
        sdk.FileApi(client).ad_video_upload(
            "test-token", body={"advertiser_id": "source-account-test"}
        )

    call_api.assert_not_called()
