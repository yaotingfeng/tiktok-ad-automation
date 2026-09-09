"""Official serialization and strict account-owned cover evidence; offline wire."""

import json
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.sdk import official_client


@pytest.fixture
def wire(monkeypatch):
    calls, replies = [], []

    def request(_pool, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return HTTPResponse(body=json.dumps(replies.pop(0)).encode(), status=200)

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    return calls, replies


def test_url_upload_uses_official_json_transport_and_typed_receipt(wire):
    from app.modules.materials.cover_sdk import upload_cover

    calls, replies = wire
    replies.append(
        {"code": 0, "data": {"image_id": "target-image", "signature": "a" * 32}}
    )
    with official_client(access_token="synthetic") as client:
        result = upload_cover(
            client,
            advertiser_id="target",
            url="https://example.com/cover",
            remote_name="cover-unique.jpg",
        )
    assert result.image_id == "target-image" and result.signature == "a" * 32
    method, url, fields = calls[0]
    assert method == "POST" and url.endswith("/file/image/ad/upload/")
    assert fields["headers"]["Content-Type"] == "application/json"
    assert json.loads(fields["body"]) == {
        "advertiser_id": "target",
        "upload_type": "UPLOAD_BY_URL",
        "image_url": "https://example.com/cover",
        "file_name": "cover-unique.jpg",
    }


def test_video_cover_url_and_suggestion_id_are_never_image_receipts(wire):
    from app.modules.materials.cover_sdk import read_video_cover, suggest_cover

    calls, replies = wire
    replies.extend(
        [
            {
                "code": 0,
                "data": {
                    "list": [
                        {
                            "video_id": "target-video",
                            "signature": "a" * 32,
                            "displayable": True,
                            "width": 720,
                            "height": 1280,
                            "video_cover_url": "http://example.com/temporary",
                        }
                    ]
                },
            },
            {
                "code": 0,
                "data": {
                    "list": [
                        {
                            "id": "not-an-uploaded-image",
                            "url": "https://example.com/suggestion",
                            "width": 360,
                            "height": 640,
                        }
                    ]
                },
            },
        ]
    )
    with official_client(access_token="synthetic") as client:
        video = read_video_cover(
            client, advertiser_id="target", video_id="target-video", md5="a" * 32
        )
        suggestion = suggest_cover(
            client,
            advertiser_id="target",
            video_id="target-video",
            width=video.width,
            height=video.height,
        )
    assert video.url == "http://example.com/temporary"
    assert "temporary" not in repr(video)
    assert suggestion == "https://example.com/suggestion"
    assert all(dict(c[2]["fields"])["advertiser_id"] == "target" for c in calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"video_id": "foreign-video"},
        {"displayable": False},
        {"signature": "b" * 32},
        {"width": 0},
        {"height": True},
    ],
)
def test_wrong_video_cannot_supply_cover(wire, changes):
    from app.modules.materials.cover_sdk import read_video_cover

    _, replies = wire
    replies.append(
        {
            "code": 0,
            "data": {
                "list": [
                    {
                        "video_id": "v",
                        "displayable": True,
                        "signature": "a" * 32,
                        "width": 720,
                        "height": 1280,
                        **changes,
                    }
                ]
            },
        }
    )
    with (
        official_client(access_token="synthetic") as client,
        pytest.raises(DomainError),
    ):
        read_video_cover(client, advertiser_id="a", video_id="v", md5="a" * 32)


def test_info_requires_exact_id_internal_name_and_displayable_geometry():
    from app.modules.materials.cover_sdk import verified_image

    image = {
        "image_id": "target",
        "file_name": "cover-owned.jpg",
        "displayable": True,
        "width": 720,
        "height": 1280,
        "signature": "a" * 32,
    }

    def verify(row):
        return verified_image(
            {"list": [row]},
            image_id="target",
            remote_name="cover-owned.jpg",
            signature="a" * 32,
            width=720,
            height=1280,
        )

    assert verify(image) == {"image_id": "target", "signature": "a" * 32}
    for change in (
        {"image_id": "source"},
        {"file_name": "foreign.jpg"},
        {"displayable": "true"},
        {"width": 1280},
        {"signature": "b" * 32},
    ):
        assert verify({**image, **change}) is None


def test_search_is_paged_and_never_retains_temporary_urls(wire):
    from app.modules.materials.cover_sdk import image_search_page, search_images

    calls, replies = wire
    replies.append(
        {
            "code": 0,
            "data": {
                "list": [
                    {
                        "image_id": "target",
                        "file_name": "cover.jpg",
                        "displayable": True,
                        "image_url": "https://example.com/private",
                        "signature": "a" * 32,
                        "width": 720,
                        "height": 1280,
                    }
                ],
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_number": 1,
                    "total_page": 1,
                },
            },
        }
    )
    with official_client(access_token="synthetic") as client:
        rows, last, total = image_search_page(
            search_images(client, advertiser_id="target", page=1), page=1
        )
    assert last and total == 1 and len(rows) == 1
    assert "image_url" not in rows[0]
    assert dict(calls[0][2]["fields"]) == {
        "advertiser_id": "target",
        "page": 1,
        "page_size": 100,
    }


@pytest.mark.parametrize(
    "rows,total,pages",
    [
        ([{"image_id": "same"}, {"image_id": "same"}], 2, 1),
        ([], 1, 1),
        ([], 10001, 101),
    ],
)
def test_incomplete_or_duplicate_image_page_cannot_prove_scan(rows, total, pages):
    from app.modules.materials.cover_sdk import image_search_page

    with pytest.raises(DomainError):
        image_search_page(
            {
                "list": rows,
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_number": total,
                    "total_page": pages,
                },
            },
            page=1,
        )


@pytest.mark.parametrize(
    "scopes, endpoint, allowed",
    [
        ([6], "UPLOAD_ENDPOINT", True),
        ([60], "UPLOAD_ENDPOINT", True),
        ([601], "UPLOAD_ENDPOINT", True),
        ([61, 611], "UPLOAD_ENDPOINT", False),
        ([601], "INFO_ENDPOINT", False),
        ([600], "INFO_ENDPOINT", True),
        ([600, 601], "VIDEO_INFO_ENDPOINT", False),
        ([610], "VIDEO_INFO_ENDPOINT", True),
        ([610], "SUGGEST_ENDPOINT", False),
        ([612], "SUGGEST_ENDPOINT", True),
        (["6"], "UPLOAD_ENDPOINT", False),
        ([True], "UPLOAD_ENDPOINT", False),
    ],
)
def test_image_and_video_oauth_leaves_are_checked_separately(
    monkeypatch, scopes, endpoint, allowed
):
    from app.core.config import settings
    from app.core.credentials import encrypt_credentials
    from app.modules.accounts.models import TikTokConnection
    from app.modules.materials import cover_sdk

    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    tenant_id = uuid4()
    connection = TikTokConnection(
        tenant_id=tenant_id,
        status="ACTIVE",
        credential_ciphertext=encrypt_credentials(
            tenant_id=tenant_id,
            value={"scope": json.dumps(scopes), "access_token": "synthetic"},
        ),
    )
    if allowed:
        cover_sdk.require_cover_scopes(
            connection, endpoint=getattr(cover_sdk, endpoint)
        )
    else:
        with pytest.raises(
            DomainError, check=lambda e: e.code == "cover_permission_unverified"
        ):
            cover_sdk.require_cover_scopes(
                connection, endpoint=getattr(cover_sdk, endpoint)
            )
