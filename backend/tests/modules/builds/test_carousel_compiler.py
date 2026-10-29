from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.integrations.tiktok.contracts.builds import (
    AdCreate,
    BuildReadQuery,
    CarouselCreative,
    VideoCreative,
)
from app.integrations.tiktok.contracts.common import CallEvidence, McpBusinessResponse
from app.modules.builds.readback_compare import compare_record, parse_page
from app.modules.builds.request_compiler import (
    create_arguments,
    decode_intent,
    encode_intent,
)


def intent(images=("image-1", "image-2", "image-3")):
    return AdCreate(
        advertiser_id="account",
        adgroup_id="group",
        name="name",
        text="copy",
        landing_page_url="https://example.com",
        portfolio_id="cta",
        identity_type="TT_USER",
        identity_id="identity",
        assets=(CarouselCreative(kind="CAROUSEL", image_ids=images, music_id="song"),),
    )


@pytest.mark.parametrize("count", [1, 3, 35])
def test_carousel_wire_roundtrip_and_sdk(count):
    value = intent(tuple(f"image-{i}" for i in range(count)))
    body = encode_intent(value)
    assert decode_intent("AD", body) == value
    info = body["creative_list"][0]["creative_info"]
    assert info["ad_format"] == "CAROUSEL_ADS"
    assert "video_info" not in info
    assert info["music_info"] == {"music_id": "song"}
    from business_api_client import ApiClient
    from business_api_client.models.smart_plus_ad_create_body import (
        SmartPlusAdCreateBody,
    )

    _, wire = create_arguments(attempt_id=uuid4(), intent=value, channel="OFFICIAL_API")
    assert ApiClient().sanitize_for_serialization(SmartPlusAdCreateBody(**wire)) == wire


@pytest.mark.parametrize("count", [0, 36])
def test_invalid_image_counts(count):
    with pytest.raises(ValidationError):
        intent(tuple("i" for _ in range(count)))


def test_music_and_conflicting_format_fields_rejected():
    with pytest.raises(ValidationError):
        CarouselCreative(kind="CAROUSEL", image_ids=("i",))
    body = encode_intent(intent())
    body["creative_list"][0]["creative_info"]["video_info"] = {"video_id": "v"}
    with pytest.raises(ValidationError):
        decode_intent("AD", body)


def observed(value, body):
    row = {**body, "smart_plus_ad_id": "ad"}
    response = McpBusinessResponse(
        data={
            "list": [row],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 1,
                "total_number": 1,
            },
        },
        evidence=CallEvidence(),
    )
    query = BuildReadQuery(intent=value, remote_id="ad")
    return compare_record(
        query=query, record=parse_page(query=query, response=response).rows[0]
    )


def test_readback_preserves_order_music_and_prohibited_fields():
    value = intent()
    body = encode_intent(value)
    assert observed(value, body) == "MATCH"
    reordered = deepcopy(body)
    reordered["creative_list"][0]["creative_info"]["image_info"].reverse()
    assert observed(value, reordered) == "MISMATCH"
    changed = deepcopy(body)
    changed["creative_list"][0]["creative_info"]["music_info"]["music_id"] = "other"
    assert observed(value, changed) == "MISMATCH"
    body["creative_list"][0]["creative_info"]["video_info"] = {"video_id": "v"}
    assert observed(value, body) == "INCOMPLETE"


def test_video_cover_semantics_preserved():
    value = intent().model_copy(
        update={
            "assets": (
                VideoCreative(
                    kind="VIDEO",
                    video_id="v",
                    cover_image_id="cover",
                    file_name="clip.mp4",
                ),
            )
        }
    )
    body = encode_intent(value)
    assert body["creative_list"][0]["creative_info"]["image_info"] == [
        {"web_uri": "cover"}
    ]
    assert decode_intent("AD", body) == value


def test_outer_creative_multiset_keeps_multiplicity_and_music_pairing():
    first = CarouselCreative(kind="CAROUSEL", image_ids=("a", "b"), music_id="first")
    second = CarouselCreative(kind="CAROUSEL", image_ids=("a", "b"), music_id="second")
    value = intent().model_copy(update={"assets": (first, second, first)})
    body = encode_intent(value)
    body["creative_list"] = [
        body["creative_list"][1],
        body["creative_list"][0],
        body["creative_list"][2],
    ]
    assert observed(value, body) == "MATCH"
    body["creative_list"].pop()
    assert observed(value, body) == "MISMATCH"
    body = encode_intent(value)
    body["creative_list"][1]["creative_info"]["music_info"]["music_id"] = "first"
    assert observed(value, body) == "MISMATCH"
    body = encode_intent(value)
    body["creative_list"][1]["creative_info"]["image_info"].reverse()
    assert observed(value, body) == "MISMATCH"


def test_reordered_video_optional_fields_and_multiplicity():
    rich = VideoCreative(
        kind="VIDEO", video_id="a", cover_image_id="cover", file_name="a.mp4"
    )
    plain = VideoCreative(kind="VIDEO", video_id="b")
    value = intent().model_copy(update={"assets": (rich, plain, rich)})
    body = encode_intent(value)
    body["creative_list"].reverse()
    body["creative_list"] = [
        body["creative_list"][1],
        body["creative_list"][0],
        body["creative_list"][2],
    ]
    assert observed(value, body) == "MATCH"
    body["creative_list"][0]["creative_info"]["image_info"] = [{"web_uri": "generated"}]
    assert observed(value, body) == "MATCH"
    body["creative_list"].pop()
    assert observed(value, body) == "MISMATCH"
    body = encode_intent(value)
    body["creative_list"][0]["creative_info"]["image_info"][0]["web_uri"] = "wrong"
    assert observed(value, body) == "MISMATCH"


def test_duplicate_video_with_optional_wildcard_matches_without_greedy_loss():
    plain = VideoCreative(kind="VIDEO", video_id="a")
    rich = VideoCreative(kind="VIDEO", video_id="a", cover_image_id="cover")
    value = intent().model_copy(update={"assets": (plain, rich)})
    body = encode_intent(value)
    body["creative_list"].reverse()
    assert observed(value, body) == "MATCH"
