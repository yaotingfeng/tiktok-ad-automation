"""图片能力与推荐限制独立；只在官方 SDK 最终 HTTP 边界提供合成回执。"""

import json
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.sdk_materials import SDKMaterialOperations
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.materials import RemoteCallBudget, URLImageUpload
from app.integrations.tiktok.sdk import official_client


@pytest.fixture
def image_sdk(monkeypatch):
    replies, calls = deque(), []
    deadline = datetime.now(UTC) + timedelta(seconds=40)
    budget = RemoteCallBudget(deadline, 45, 60000)

    @contextmanager
    def scope(_advertiser, _operation, actual_deadline):
        assert actual_deadline == deadline
        yield

    def http(_pool, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": replies.popleft(), "request_id": "image-evidence"}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.connectionpool.HTTPSConnectionPool.urlopen", http)
    with official_client(access_token="synthetic-image-token") as client:

        def opened(scopes=frozenset({6})):
            return SDKMaterialOperations(
                client, request_scope=scope, deadline=deadline, api_scope_ids=scopes
            )

        yield opened, replies, calls, budget


def test_image_capability_is_not_inferred_from_video(image_sdk):
    opened, replies, _, budget = image_sdk
    # 能用于视频封面的可展示图片，也不能证明能作为轮播主图。
    replies.append({"list": [{"image_id": "image-1", "displayable": True}]})
    record = opened().read_images(
        advertiser_id="123", image_ids=("image-1",), budget=budget
    )[0]
    assert asdict(record).get("is_carousel_usable", "missing") is None


def test_video_permission_does_not_allow_image_upload(image_sdk):
    opened, _, calls, budget = image_sdk
    with pytest.raises(RemoteCallError) as error:
        opened(frozenset({61, 611})).upload_image_url(
            URLImageUpload("123", "https://cdn.example/image.jpg", "image.jpg"),
            budget=budget,
        )
    assert error.value.code == "cover_permission_unverified"
    assert error.value.effect == "NOT_SENT"
    assert calls == []


@pytest.mark.parametrize("usable", [True, False, None])
@pytest.mark.parametrize("operation", ["read", "search"])
def test_image_read_preserves_carousel_evidence(image_sdk, usable, operation):
    opened, replies, _, budget = image_sdk
    replies.append(
        {
            "list": [
                {
                    "image_id": "image-1",
                    "is_carousel_usable": usable,
                    "displayable": not usable,
                    "material_id": "mid-1",
                }
            ],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 1,
                "total_number": 1,
            },
        }
    )
    adapter = opened()
    if operation == "read":
        record = adapter.read_images(
            advertiser_id="123", image_ids=("image-1",), budget=budget
        )[0]
    else:
        record = adapter.search_images(
            advertiser_id="123", image_ids=("image-1",), page=1, budget=budget
        ).rows[0]
    assert asdict(record).get("is_carousel_usable", "missing") is usable
    assert record.advertiser_id == "123" and record.mid == "mid-1"
    assert record.evidence.request_id == "image-evidence"


@pytest.mark.parametrize("invalid", [1, 0, "true", "false", [], {}])
@pytest.mark.parametrize("operation", ["read", "search"])
def test_carousel_evidence_rejects_non_boolean_values(image_sdk, invalid, operation):
    opened, replies, _, budget = image_sdk
    replies.append(
        {
            "list": [{"image_id": "image-1", "is_carousel_usable": invalid}],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 1,
                "total_number": 1,
            },
        }
    )
    with pytest.raises(DomainError) as error:
        if operation == "read":
            opened().read_images(
                advertiser_id="123", image_ids=("image-1",), budget=budget
            )
        else:
            opened().search_images(
                advertiser_id="123", image_ids=("image-1",), page=1, budget=budget
            )
    assert error.value.code == "unsupported_material_schema"


def test_upload_and_recommend_limits_are_independent(image_sdk):
    opened, replies, calls, budget = image_sdk
    # 这是合成上传/查询成功事实，不声称平台已实测接受此尺寸。
    # 50 MB 或推荐尺寸约束不能被错误加入通用上传及图片读取合同。
    replies.append({"image_id": "large-image", "width": 4000, "height": 4000})
    adapter = opened()
    receipt = adapter.upload_image_url(
        URLImageUpload("123", "https://cdn.example/large.png", "large.png"),
        budget=budget,
    )
    assert receipt.image_id == "large-image"
    replies.append(
        {
            "list": [
                {
                    "image_id": "large-image",
                    "width": 4000,
                    "height": 4000,
                    "size": 60 * 1024 * 1024,
                    "is_carousel_usable": False,
                }
            ]
        }
    )
    record = adapter.read_images(
        advertiser_id="123", image_ids=("large-image",), budget=budget
    )[0]
    assert record.width == 4000 and record.height == 4000
    assert asdict(record).get("is_carousel_usable", "missing") is False
    assert len(calls) == 2
