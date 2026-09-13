"""Business asset composition, actual official wire shape, no remote services."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.sdk import official_client
from app.modules.builds.request_compiler import cta_portfolio, decode_intent
from tests.integrations.tiktok.build_wire import sdk_build_operations


def test_each_sp_uses_complete_target_group_and_one_independent_text():
    from app.modules.builds.request_compiler import ad_assets

    mappings = [
        {
            "video_id": "target-v1",
            "image_id": "target-cover-1",
            "file_name": "01-The General-CL6-LH-1.mp4",
        },
        {"video_id": "target-v2", "image_id": "target-cover-2"},
    ]
    identity = {
        "identity_type": "BC_AUTH_TT",
        "identity_id": "identity-1",
        "identity_authorized_bc_id": "bc-1",
    }
    before = deepcopy((mappings, identity))
    a = ad_assets(
        mappings,
        text="Watch an episode.",
        url="https://example.test/minis",
        identity=identity,
    )
    b = ad_assets(
        mappings,
        text="Follow the story.",
        url="https://example.test/minis",
        identity=identity,
    )
    assert a["creative_list"] == b["creative_list"]
    assert [
        c["creative_info"]["video_info"]["video_id"] for c in a["creative_list"]
    ] == ["target-v1", "target-v2"]
    assert [c["creative_info"]["image_info"] for c in a["creative_list"]] == [
        [{"web_uri": "target-cover-1"}],
        [{"web_uri": "target-cover-2"}],
    ]
    assert (
        a["creative_list"][0]["creative_info"]["video_info"]["file_name"]
        == "01-The General-CL6-LH-1.mp4"
    )
    from app.modules.builds.request_compiler import encode_intent

    request = {
        **a,
        "advertiser_id": "account",
        "adgroup_id": "group",
        "ad_name": "ad",
        "operation_status": "ENABLE",
        "ad_configuration": {"call_to_action_id": "cta"},
    }
    assert encode_intent(decode_intent("AD", request)) == request
    assert a["ad_text_list"] == [{"ad_text": "Watch an episode."}]
    assert b["ad_text_list"] == [{"ad_text": "Follow the story."}]
    assert (mappings, identity) == before


@pytest.mark.parametrize(
    "mappings",
    [
        [],
        [{"video_id": "source-only"}],
        [{"video_id": "target", "image_id": True}],
        [{"video_id": " ", "image_id": "cover"}],
    ],
)
def test_unverified_target_mapping_never_compiles(mappings):
    from app.modules.builds.request_compiler import ad_assets

    with pytest.raises(DomainError):
        ad_assets(
            mappings,
            text="Watch.",
            url="https://example.test",
            identity={
                "identity_type": "BC_AUTH_TT",
                "identity_id": "identity",
                "identity_authorized_bc_id": "bc",
            },
        )


def test_identity_cannot_override_creative_assets():
    from app.modules.builds.request_compiler import ad_assets

    with pytest.raises(DomainError):
        ad_assets(
            [{"video_id": "target", "image_id": "cover"}],
            text="Watch.",
            url="https://example.test",
            identity={
                "identity_type": "BC_AUTH_TT",
                "identity_id": "identity",
                "identity_authorized_bc_id": "bc",
                "video_info": {"video_id": "source"},
            },
        )


def test_official_cta_portfolio_wire_uses_actual_recommendation_ids(monkeypatch):

    calls = []

    def request(_pool, method, url, **kwargs):
        calls.append((method, url, json.loads(kwargs["body"])))
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {"creative_portfolio_id": "portfolio-1"},
                    "request_id": "receipt-1",
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    body = cta_portfolio(
        advertiser_id="advertiser-1",
        assets=(
            {
                "asset_ids": ["actual-cta-1", "actual-cta-2"],
                "asset_content": "Watch now",
            },
        ),
    )
    with official_client(access_token="fixture-token") as client:
        result = sdk_build_operations(client).create(
            attempt_id=uuid4(), intent=decode_intent("CTA", body)
        )
    assert result.remote_id == "portfolio-1"
    assert calls == [
        (
            "POST",
            "https://business-api.tiktok.com/open_api/v1.3/creative/portfolio/create/",
            {
                "advertiser_id": "advertiser-1",
                "creative_portfolio_type": "CTA",
                "portfolio_content": [
                    {
                        "asset_ids": ["actual-cta-1", "actual-cta-2"],
                        "asset_content": "Watch now",
                    }
                ],
            },
        )
    ]


def test_portfolio_unknown_response_is_not_retryable_proof(monkeypatch):

    calls = []

    def request(_pool, method, _url, **_kwargs):
        calls.append(method)
        return HTTPResponse(
            body=b'{"code":0,"data":{},"request_id":"receipt-1"}', status=200
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    with official_client(access_token="fixture-token") as client:
        with pytest.raises(RemoteCallError) as caught:
            sdk_build_operations(client).create(
                attempt_id=uuid4(),
                intent=decode_intent(
                    "CTA",
                    cta_portfolio(
                        advertiser_id="advertiser-1",
                        assets=(
                            {
                                "asset_ids": ["actual-cta-1"],
                                "asset_content": "Watch now",
                            },
                        ),
                    ),
                ),
            )
    assert (
        caught.value.effect == "UNKNOWN"
        and caught.value.evidence.request_id == "receipt-1"
        and calls == ["POST"]
    )
