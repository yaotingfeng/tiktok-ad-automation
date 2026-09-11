"""冻结业务字段必须完整往返，不能由编码时钟或默认值修补远端事实。"""

from copy import deepcopy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.integrations.tiktok.contracts.builds import CampaignCreate, CtaCreate
from app.modules.builds.request_compiler import decode_intent, encode_intent


def build_bodies():
    return {
        "CAMPAIGN": {
            "advertiser_id": "90071992547409939999",
            "campaign_name": "冻结名称",
            "budget": "100.01",
            "objective_type": "APP_PROMOTION",
            "app_promotion_type": "MINIS",
            "campaign_type": "REGULAR_CAMPAIGN",
            "catalog_enabled": False,
            "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
            "budget_optimize_on": True,
            "operation_status": "ENABLE",
        },
        "ADGROUP": {
            "advertiser_id": "90071992547409939999",
            "campaign_id": "90071992547409939998",
            "adgroup_name": "冻结广告组",
            "roas_bid": "1.234567890123456789",
            "minis_id": "minis-1",
            "promotion_type": "MINI_APP",
            "optimization_goal": "VALUE",
            "optimization_event": "ACTIVE_PAY",
            "bid_type": "BID_TYPE_NO_BID",
            "deep_bid_type": "VO_MIN_ROAS",
            "billing_event": "OCPM",
            "placement_type": "PLACEMENT_TYPE_NORMAL",
            "placements": ["PLACEMENT_TIKTOK"],
            "targeting_spec": {"location_ids": ["region-2", "region-1"]},
            "schedule_type": "SCHEDULE_FROM_NOW",
            "schedule_start_time": "2026-09-12 01:02:03",
            "operation_status": "ENABLE",
        },
        "AD": {
            "advertiser_id": "90071992547409939999",
            "adgroup_id": "90071992547409939997",
            "ad_name": "冻结广告",
            "operation_status": "ENABLE",
            "creative_list": [
                {
                    "creative_info": {
                        "identity_type": "BC_AUTH_TT",
                        "identity_id": "identity-1",
                        "identity_authorized_bc_id": "bc-1",
                        "ad_format": "SINGLE_VIDEO",
                        "video_info": {"video_id": "target-video"},
                        "image_info": [{"web_uri": "target-cover"}],
                    },
                }
            ],
            "ad_text_list": [{"ad_text": "开始观看"}],
            "landing_page_url_list": [
                {"landing_page_url": "https://example.invalid/minis"}
            ],
            "ad_configuration": {"call_to_action_id": "portfolio-1"},
        },
        "CTA": {
            "advertiser_id": "90071992547409939999",
            "creative_portfolio_type": "CTA",
            "portfolio_content": [
                {"asset_ids": ["cta-1", "cta-2"], "asset_content": "立即观看"}
            ],
        },
    }


def test_campaign_keeps_exact_values_and_direct_enable():
    value = CampaignCreate(
        advertiser_id="90071992547409939999", name="冻结名称", budget=Decimal("100.01")
    )
    assert value.advertiser_id == "90071992547409939999"
    assert value.budget == Decimal("100.01")
    assert value.operation_status == "ENABLE"
    for change in (
        {"operation_status": "DISABLE"},
        {"unverified_option": True},
        {"advertiser_id": 123},
        {"advertiser_id": " "},
        {"budget": "NaN"},
        {"budget": "0"},
        {"budget": True},
    ):
        with pytest.raises(ValidationError):
            CampaignCreate(**{**value.model_dump(), **change})


@pytest.mark.parametrize("kind", ["CAMPAIGN", "ADGROUP", "AD", "CTA"])
def test_full_actual_request_roundtrip_preserves_fields_without_clock(kind):
    body = build_bodies()[kind]
    original = deepcopy(body)
    value = decode_intent(kind, body)
    assert encode_intent(value) == original
    assert body == original
    assert encode_intent(decode_intent(kind, encode_intent(value))) == original
    with pytest.raises(ValidationError):
        value.advertiser_id = "changed"


@pytest.mark.parametrize("kind", ["CAMPAIGN", "ADGROUP", "AD", "CTA"])
def test_unknown_wire_fields_cannot_escape_typed_contract(kind):
    body = build_bodies()[kind]
    with pytest.raises(ValidationError):
        decode_intent(kind, {**body, "unverified_option": True})


@pytest.mark.parametrize(
    "value",
    [
        "2026-02-30 01:02:03",
        "2026-9-12 01:02:03",
        "2026-09-12T01:02:03Z",
        "2026-09-12 25:02:03",
    ],
)
def test_schedule_requires_exact_valid_utc_wall_time(value):
    body = build_bodies()["ADGROUP"]
    body["schedule_start_time"] = value
    with pytest.raises(ValidationError):
        decode_intent("ADGROUP", body)


def test_nested_creative_fields_and_target_asset_identity_are_strict():
    for change in (
        {"unverified": True},
        {"image_info": [{"web_uri": ""}]},
        {"identity_id": ""},
        {"video_info": {"video_id": 123}},
    ):
        body = build_bodies()["AD"]
        body["creative_list"][0]["creative_info"].update(change)
        with pytest.raises(ValidationError):
            decode_intent("AD", body)


def test_cta_union_cannot_exceed_fifty_actual_selection_ids():
    with pytest.raises(ValidationError):
        CtaCreate(
            advertiser_id="123",
            assets=(
                {
                    "asset_ids": tuple(str(i) for i in range(50)),
                    "asset_content": "立即观看",
                },
                {"asset_ids": ("one-more",), "asset_content": "了解详情"},
            ),
        )


@pytest.mark.parametrize(
    "kind,id_key",
    [
        ("CAMPAIGN", "campaign_id"),
        ("ADGROUP", "adgroup_id"),
        ("AD", "smart_plus_ad_id"),
        ("CTA", "creative_portfolio_id"),
    ],
)
def test_readback_requires_all_actual_fields_and_compares_parent_money_assets(
    kind, id_key
):
    from app.integrations.tiktok.contracts.builds import BuildReadQuery
    from app.integrations.tiktok.contracts.common import (
        CallEvidence,
        McpBusinessResponse,
    )
    from app.modules.builds.readback_compare import compare_record, parse_page

    body = build_bodies()[kind]
    query = BuildReadQuery(intent=decode_intent(kind, body), remote_id="remote-1")
    row = {**body, id_key: "remote-1"}

    def read(value):
        data = (
            value
            if kind == "CTA"
            else {
                "list": [value],
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_number": 1,
                    "total_page": 1,
                },
            }
        )
        return parse_page(
            query=query,
            response=McpBusinessResponse(data, CallEvidence(request_id="safe-request")),
        )

    result = read(row)
    assert result.complete and result.evidence.request_id == "safe-request"
    assert compare_record(query=query, record=result.rows[0]) == "MATCH"
    for missing in body:
        incomplete = deepcopy(row)
        del incomplete[missing]
        record = read(incomplete).rows[0]
        assert record.intent is None and missing in record.missing_fields
        assert compare_record(query=query, record=record) == "INCOMPLETE"
    altered = deepcopy(row)
    if kind == "CAMPAIGN":
        altered["budget"] = "100.010000000000000001"
    elif kind == "ADGROUP":
        altered["campaign_id"] = "wrong-parent"
    elif kind == "AD":
        altered["creative_list"][0]["creative_info"]["image_info"][0]["web_uri"] = (
            "wrong-cover"
        )
    else:
        altered["portfolio_content"][0]["asset_ids"] = ["unselected-cta"]
    assert compare_record(query=query, record=read(altered).rows[0]) == "MISMATCH"


def test_explicit_disabled_status_wins_over_incomplete_fields():
    from app.integrations.tiktok.contracts.builds import BuildReadQuery
    from app.integrations.tiktok.contracts.common import (
        CallEvidence,
        McpBusinessResponse,
    )
    from app.modules.builds.readback_compare import compare_record, parse_page

    query = BuildReadQuery(intent=decode_intent("CAMPAIGN", build_bodies()["CAMPAIGN"]))
    data = {
        "list": [{"campaign_id": "remote", "operation_status": "DISABLE"}],
        "page_info": {"page": 1, "page_size": 100, "total_number": 1, "total_page": 1},
    }
    record = parse_page(
        query=query, response=McpBusinessResponse(data, CallEvidence())
    ).rows[0]
    assert record.operation_status == "DISABLE" and record.intent is None
    assert compare_record(query=query, record=record) == "MISMATCH"


def test_boolean_settings_and_invalid_decimal_are_not_coerced():
    for change in (
        {"catalog_enabled": 0},
        {"budget_optimize_on": 1},
        {"budget": "not-a-number"},
    ):
        with pytest.raises(ValidationError):
            decode_intent("CAMPAIGN", {**build_bodies()["CAMPAIGN"], **change})


def test_positive_row_count_requires_positive_page_count():
    from app.integrations.tiktok.contracts.builds import BuildReadQuery
    from app.integrations.tiktok.contracts.common import (
        CallEvidence,
        McpBusinessResponse,
        RemoteCallError,
    )
    from app.modules.builds.readback_compare import parse_page

    body = build_bodies()["CAMPAIGN"]
    data = {
        "list": [{**body, "campaign_id": "campaign-1"}],
        "page_info": {"page": 1, "page_size": 100, "total_number": 1, "total_page": 0},
    }
    with pytest.raises(RemoteCallError):
        parse_page(
            query=BuildReadQuery(intent=decode_intent("CAMPAIGN", body)),
            response=McpBusinessResponse(data, CallEvidence()),
        )


def test_current_scene_and_legacy_compiler_keep_every_actual_field():
    from types import SimpleNamespace

    from app.modules.builds.scene import _assemble_scene
    from app.modules.builds.sdk_requests import (
        ad_assets,
        compile_request,
        cta_portfolio,
    )

    scene = _assemble_scene(
        scope={"access": SimpleNamespace(currency="USD"), "basis": "synthetic"},
        facts={
            "minis": {
                "matches": [
                    {
                        "status": "ACTIVE",
                        "type": "MINI_SERIES",
                        "minis_id": "minis-1",
                        "regions": ["US"],
                    }
                ]
            },
            "regions": {
                "locations": [{"region_code": "US", "location_id": "region-1"}]
            },
            "identity": {
                "matches": [
                    {
                        "identity_type": "BC_AUTH_TT",
                        "identity_id": "identity-1",
                        "identity_authorized_bc_id": "bc-1",
                    }
                ]
            },
            "cta": {
                "asset_ids": ["cta-1"],
                "recommend_assets": [
                    {"asset_ids": ["cta-1"], "asset_content": "立即观看"}
                ],
            },
            "vbo": {"vo_min_roas": "QUALIFIED"},
        },
        reasons=[],
        evidence_ids=(),
        capability=SimpleNamespace(scope_verified=True, can_build=True),
        locally_operable=True,
    )
    assert scene.supported
    campaign = compile_request(
        "campaign",
        fixed={"advertiser_id": "123", "campaign_name": "campaign", "budget": "100.01"},
        resolved=scene.campaign_fields,
    )
    group = compile_request(
        "adgroup",
        fixed={
            "advertiser_id": "123",
            "campaign_id": "campaign-1",
            "adgroup_name": "group",
            "roas_bid": "1.50",
        },
        resolved={
            **scene.adgroup_fields,
            "schedule_type": "SCHEDULE_FROM_NOW",
            "schedule_start_time": "2026-09-12 01:02:03",
        },
    )
    creative = ad_assets(
        [{"video_id": "video", "image_id": "cover"}],
        text="观看",
        url="https://example.invalid",
        identity={
            key: value
            for key, value in scene.creative_fields["creative_info"].items()
            if key != "ad_format"
        },
    )
    ad = compile_request(
        "ad",
        fixed={"advertiser_id": "123", "adgroup_id": "group-1", "ad_name": "ad"},
        resolved={**creative, "ad_configuration": {"call_to_action_id": "portfolio-1"}},
    )
    cta = cta_portfolio(
        advertiser_id="123", assets=scene.cta_fields["recommend_assets"]
    )
    for kind, body in (
        ("CAMPAIGN", campaign),
        ("ADGROUP", group),
        ("AD", ad),
        ("CTA", cta),
    ):
        assert encode_intent(decode_intent(kind, body)) == body


@pytest.mark.parametrize(
    "kind,field", [("CAMPAIGN", "budget"), ("ADGROUP", "roas_bid")]
)
def test_legacy_numeric_input_is_separate_from_remote_money_evidence(kind, field):
    from app.modules.builds.request_compiler import decode_observed_intent

    body = {**build_bodies()[kind], field: 100.01}
    assert getattr(decode_intent(kind, body), field) == Decimal("100.01")
    with pytest.raises(ValidationError):
        decode_observed_intent(kind, body)
    for exact in ("100.01", Decimal("100.01"), 100):
        value = decode_observed_intent(kind, {**body, field: exact})
        assert getattr(value, field) == Decimal(str(exact))
