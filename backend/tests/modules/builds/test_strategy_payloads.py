"""预算/竞价策略的离线请求合同回归。"""

import pytest

from app.core.errors import DomainError
from app.modules.builds.preview_validation import scene_reasons
from app.modules.builds.request_compiler import compile_request, decode_intent
from app.modules.builds.scene_schemas import SceneContext
from app.modules.strategies.schemas import StrategyConfig


def _config(**changes):
    from uuid import uuid4

    return StrategyConfig(
        budget="100.00",
        currency="USD",
        copy_pool_version=uuid4(),
        cta_option_ids=("cta",),
        **changes,
    )


def _scene(**constraints):
    return SceneContext(
        supported=True,
        reason_codes=(),
        capability_revision="fixture",
        creative_limit=50,
        copy_length_limit=100,
        cta_fields={"asset_ids": ["cta"]},
        field_constraints={
            "max_ads_per_adgroup": 30,
            "campaign_daily_budget": {
                "currency": "USD",
                "minimum_inclusive": "50",
                "maximum_exclusive": "10000000",
                "precision": "0.01",
            },
            "roas_bid": {"minimum": "0.01", "maximum": "1000"},
            "bid_capabilities": {
                "HIGHEST_VALUE": {
                    "optimization_goal": "VALUE",
                    "optimization_event": "AD_REVENUE_VALUE",
                    "deep_bid_type": "VO_HIGHEST_VALUE",
                },
                "TARGET_ROAS": {"deep_bid_type": "VO_MIN_ROAS"},
            },
            **constraints,
        },
    )


def test_series_and_group_budget_are_emitted_at_their_own_layer():
    series = compile_request(
        "campaign",
        fixed={
            "advertiser_id": "adv",
            "campaign_name": "series",
            "budget": 100,
            "budget_strategy": "SERIES",
        },
        resolved={"objective_type": "APP_PROMOTION"},
    )
    assert series["budget"] == 100
    assert series["budget_mode"] == "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"

    group = compile_request(
        "adgroup",
        fixed={
            "advertiser_id": "adv",
            "campaign_id": "campaign",
            "adgroup_name": "group",
            "budget": 100,
            "budget_strategy": "ADGROUP",
            "bid_strategy": "HIGHEST_VALUE",
        },
        resolved={"targeting_spec": {"location_ids": ["US"]}},
    )
    assert group["budget"] == 100
    assert "budget" not in compile_request(
        "campaign",
        fixed={
            "advertiser_id": "adv",
            "campaign_name": "series",
            "budget_strategy": "ADGROUP",
        },
        resolved={"objective_type": "APP_PROMOTION"},
    )


def test_bid_payloads_separate_highest_value_and_target_roas():
    highest = compile_request(
        "adgroup",
        fixed={
            "advertiser_id": "adv",
            "campaign_id": "campaign",
            "adgroup_name": "group",
            "budget_strategy": "SERIES",
            "bid_strategy": "HIGHEST_VALUE",
        },
        resolved={"optimization_event": "ACTIVE_PAY"},
    )
    assert highest["optimization_event"] == "AD_REVENUE_VALUE"
    assert highest["deep_bid_type"] == "VO_HIGHEST_VALUE"
    assert "roas_bid" not in highest

    target = compile_request(
        "adgroup",
        fixed={
            "advertiser_id": "adv",
            "campaign_id": "campaign",
            "adgroup_name": "group",
            "budget_strategy": "SERIES",
            "bid_strategy": "TARGET_ROAS",
            "roas_bid": 1.08,
        },
        resolved={"optimization_event": "ACTIVE_PAY"},
    )
    assert target["deep_bid_type"] == "VO_MIN_ROAS"
    assert target["roas_bid"] == 1.08


@pytest.mark.parametrize("event", ["ACTIVE_PAY", "IMPRESSION_LEVEL_AD_REVENUE"])
def test_readback_event_variants_map_to_the_same_highest_value_strategy(event):
    body = {
        "advertiser_id": "adv",
        "campaign_id": "campaign",
        "adgroup_name": "group",
        "minis_id": "minis",
        "optimization_goal": "VALUE",
        "optimization_event": event,
        "bid_type": "BID_TYPE_NO_BID",
        "deep_bid_type": "VO_HIGHEST_VALUE",
        "billing_event": "OCPM",
        "placement_type": "PLACEMENT_TYPE_NORMAL",
        "placements": ["PLACEMENT_TIKTOK"],
        "targeting_spec": {"location_ids": ["US"]},
        "schedule_type": "SCHEDULE_FROM_NOW",
        "schedule_start_time": "2026-10-04 00:00:00",
        "operation_status": "ENABLE",
    }
    intent = decode_intent("ADGROUP", body)
    assert intent.bid_strategy == "HIGHEST_VALUE"


def test_unsupported_group_budget_is_a_preview_blocker():
    reasons = scene_reasons(
        _config(budget_strategy="ADGROUP"),
        _scene(),
        "USD",
    )
    assert "adgroup_budget_unverified" in reasons


@pytest.mark.parametrize(
    "fixed,code",
    [
        (
            {
                "advertiser_id": "adv",
                "campaign_name": "x",
                "budget_strategy": "UNKNOWN",
            },
            "invalid_budget_strategy",
        ),
        (
            {
                "advertiser_id": "adv",
                "campaign_id": "c",
                "adgroup_name": "x",
                "budget_strategy": "SERIES",
                "bid_strategy": "TARGET_ROAS",
            },
            "bid_strategy_invalid",
        ),
    ],
)
def test_unknown_strategy_is_rejected_before_remote_call(fixed, code):
    with pytest.raises(DomainError) as error:
        compile_request(
            "campaign" if "campaign_name" in fixed else "adgroup",
            fixed=fixed,
            resolved={},
        )
    assert error.value.code == code
