"""真实官方SDK在传输边界校验帖子/素材原始内容证据及物理调用准入。"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.sdk_report_reader import SdkReportReader
from app.integrations.tiktok.gateway import _sdk_report_send
from app.integrations.tiktok.sdk import AccountAdmissionDeferred, official_client

CONTENT = {"signature": "a" * 32, "size": 123}
ARGS = {
    "advertiser_id": "a",
    "item_id": "post",
    "identity_type": "BC_AUTH_TT",
    "identity_id": "identity",
    "identity_authorized_bc_id": "bc",
}


def test_official_wire_keeps_scope_and_compares_raw_content(monkeypatch):
    calls, admissions = [], []

    def wire(_pool, method, url, **kwargs):
        fields = dict(kwargs["fields"])
        calls.append((method, url, fields))
        if url.endswith("/identity/video/info/"):
            assert all(fields[k] == v for k, v in ARGS.items())
            body = {
                "video_detail": {
                    "item_id": "post",
                    "item_type": "VIDEO",
                    "video_info": CONTENT,
                }
            }
        else:
            assert url.endswith("/file/video/ad/info/")
            assert fields["advertiser_id"] == "a" and json.loads(
                fields["video_ids"]
            ) == ["vid"]
            body = {"list": [{"video_id": "vid", **CONTENT}]}
        return HTTPResponse(
            body=json.dumps({"code": 0, "data": body}).encode(), status=200
        )

    @contextmanager
    def scope(adv, op, _deadline):
        admissions.append((adv, op))
        yield

    monkeypatch.setattr("urllib3.PoolManager.request", wire)
    with official_client(access_token="synthetic-test") as client:
        reader = SdkReportReader(
            send=_sdk_report_send(
                client, scope, datetime.now(UTC) + timedelta(seconds=30)
            )
        )
        assert reader.spark_video_content(**ARGS) == CONTENT
        assert reader.material_video_content(advertiser_id="a", video_ids=("vid",)) == [
            {"video_id": "vid", **CONTENT}
        ]
        assert reader.request_count == 2
    assert admissions == [
        ("a", "scene.get_identity_video"),
        ("a", "materials.get_videos"),
    ]
    assert all(c[0] == "GET" for c in calls)


@pytest.mark.parametrize(
    "body",
    [
        {"item_id": "foreign", "item_type": "VIDEO", "video_info": CONTENT},
        {
            "item_id": "post",
            "item_type": "VIDEO",
            "video_info": {"signature": "bad", "size": 123},
        },
        {
            "item_id": "post",
            "item_type": "VIDEO",
            "video_info": {"signature": "a" * 32, "size": True},
        },
    ],
)
def test_post_identity_and_content_cannot_be_inferred_from_request(body):
    reader = SdkReportReader(send=lambda *_: {"video_detail": body})
    with pytest.raises(DomainError):
        reader.spark_video_content(**ARGS)


@pytest.mark.parametrize(
    "row",
    [
        {"video_id": "foreign", **CONTENT},
        {"video_id": "vid", "advertiser_id": "foreign", **CONTENT},
        {"video_id": "vid", "signature": "bad", "size": 123},
    ],
)
def test_candidate_asset_scope_must_be_proven(row):
    reader = SdkReportReader(send=lambda *_: {"list": [row]})
    with pytest.raises(DomainError):
        reader.material_video_content(advertiser_id="a", video_ids=("vid",))


@pytest.mark.parametrize(
    "code,message,expected",
    [
        (40000, "Unable to fetch a valid item", None),
        (40000, "Invalid identity", "tiktok_response_error"),
        (40100, "QPM exceeded", "admission_deferred"),
    ],
)
def test_only_explicit_missing_item_becomes_unavailable(
    monkeypatch, code, message, expected
):
    from contextlib import nullcontext

    monkeypatch.setattr(
        "urllib3.PoolManager.request",
        lambda *_args, **_kwargs: HTTPResponse(
            body=json.dumps({"code": code, "message": message, "data": {}}).encode(),
            status=200,
        ),
    )
    with official_client(access_token="synthetic-test") as client:
        reader = SdkReportReader(
            send=_sdk_report_send(
                client,
                lambda *_: nullcontext(),
                datetime.now(UTC) + timedelta(seconds=30),
            )
        )
        if expected is None:
            assert reader.spark_video_content(**ARGS) is None
        else:
            with pytest.raises(DomainError) as caught:
                reader.spark_video_content(**ARGS)
            assert caught.value.code == expected
            if code == 40100:
                assert isinstance(caught.value, AccountAdmissionDeferred)
