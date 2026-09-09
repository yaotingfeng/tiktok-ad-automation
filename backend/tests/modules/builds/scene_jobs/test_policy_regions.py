"""Official doc1737189539571713 wire fields; application copy policy is separate."""

import json

import business_api_client as sdk
import pytest

from app.core.errors import DomainError
from app.modules.builds import scene_constraints, scene_sdk


def test_unknown_platform_copy_max_does_not_block_application_policy():
    constraints, reasons = scene_constraints.constraints_for("USD")
    assert constraints.get("platform_copy_length") is None
    assert constraints["copy_length"] == 100
    assert constraints["copy_measurement"] == "characters"
    assert constraints["copy_policy"]["source"] == "APPLICATION"
    assert constraints["copy_policy"]["minimum"] == 1
    assert "field_limits_unverified" not in reasons


def test_country_request_preserves_documented_minis_fields_in_official_sdk(monkeypatch):
    client = sdk.ApiClient()
    client.default_headers["Access-Token"] = "offline-regions-token"
    captured = {}

    def transport(path, method, *args, **kwargs):
        captured.update(
            path=path,
            method=method,
            query=dict(args[1]),
            timeout=kwargs["_request_timeout"],
        )
        return sdk.InlineResponse200(code=0, data={})

    monkeypatch.setattr(client, "call_api", transport)
    assert "regions" in scene_sdk.ENDPOINTS, "Documented targeting source is missing"
    scene_sdk.request_page(
        client,
        resource="regions",
        bc_id="offline-bc",
        advertiser_id="offline-account",
        page=1,
    )
    assert captured["path"] == "/open_api/v1.3/tool/region/"
    assert captured["method"] == "GET"
    assert captured["query"] == {
        "advertiser_id": "offline-account",
        "placements": json.dumps(["PLACEMENT_TIKTOK"]),
        "objective_type": "APP_PROMOTION",
        "app_promotion_type": "MINIS",
        "promotion_type": "MINI_APP",
        "level_range": "TO_COUNTRY",
        "language": "en",
    }
    assert captured["timeout"] == (5, 30)


def region_response():
    return {
        "code": 0,
        "request_id": "offline-regions",
        "data": {
            "region_list": ["US", "CA"],
            "region_info": [
                {
                    "region_code": "US",
                    "location_id": "6252001",
                    "level": "COUNTRY",
                    "area_type": "ADMIN",
                },
                {
                    "region_code": "CA",
                    "location_id": "6251999",
                    "level": "COUNTRY",
                    "area_type": "ADMIN",
                },
            ],
        },
    }


def parse(response):
    return scene_sdk.parse_page(
        sdk.InlineResponse200(**response),
        resource="regions",
        page=1,
        advertiser_id="offline-account",
        bc_id="offline-bc",
        minis_id="offline-minis",
    )


def test_complete_country_mapping_retains_real_location_ids():
    assert "regions" in scene_sdk.ENDPOINTS
    facts, complete, request_id = parse(region_response())
    assert facts["locations"] == [
        {"region_code": "CA", "location_id": "6251999"},
        {"region_code": "US", "location_id": "6252001"},
    ]
    assert complete and request_id == "offline-regions"


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_code",
        "duplicate_id",
        "missing_country",
        "wrong_level",
        "country_as_id",
        "oversized",
    ],
)
def test_country_mapping_fails_closed_on_incomplete_or_ambiguous_data(case):
    assert "regions" in scene_sdk.ENDPOINTS
    response = region_response()
    items = response["data"]["region_info"]
    if case == "duplicate_code":
        items[1]["region_code"] = "US"
    elif case == "duplicate_id":
        items[1]["location_id"] = items[0]["location_id"]
    elif case == "missing_country":
        items.pop()
    elif case == "wrong_level":
        items[0]["level"] = "CITY"
    elif case == "country_as_id":
        items[0]["location_id"] = "US"
    else:
        response["data"]["region_info"] = items * 151
    with pytest.raises(DomainError) as error:
        parse(response)
    assert error.value.code == "scene_response_unverified"
