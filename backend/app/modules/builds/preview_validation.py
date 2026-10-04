"""Apply the verified scene contract; never infer platform limits from defaults."""

import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from app.modules.builds.scene_schemas import SceneContext
from app.modules.strategies.schemas import StrategyConfig


def measured(text: str, method: str) -> int:
    if method == "characters":
        return len(text)
    if method == "cjk_weighted":
        # The official rule assigns Chinese/Japanese/Korean characters two units.
        return sum(
            2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in text
        )
    raise ValueError("Unknown measurement")


def name_reasons(name: str, kind: str, scene: dict[str, Any]) -> list[str]:
    limits = scene.get("field_constraints", {})
    maximum = limits.get("name_limits", {}).get(kind)
    method = limits.get("name_measurement", {}).get(kind)
    if (
        type(maximum) is not int
        or maximum <= 0
        or method not in {"characters", "cjk_weighted"}
    ):
        return ["field_limits_unverified"]
    reasons = []
    if measured(name, method) > maximum:
        reasons.append("name_too_long")
    if any(unicodedata.category(c) in {"Cc", "Cs"} for c in name):
        reasons.append("name_invalid")
    if limits.get("emoji_allowed") is False and any(
        unicodedata.category(c) == "So" or c in "\ufe0f\u200d\u20e3" for c in name
    ):
        reasons.append("name_invalid")
    return reasons


def final_ad_count_exceeded(
    *, base_ad_count: int, creative_count: int, maximum: Any
) -> bool:
    """Check the platform limit against the final copied-ad count per group."""
    return (
        type(maximum) is int
        and maximum > 0
        and base_ad_count * creative_count > maximum
    )


def scene_reasons(
    config: StrategyConfig, scene: SceneContext, currency: str
) -> list[str]:
    reasons = list(scene.reason_codes)
    if not scene.supported and not reasons:
        reasons.append("scene_unsupported")
    if currency != config.currency:
        reasons.append("currency_mismatch")
    c = scene.field_constraints
    maximum = c.get("max_ads_per_adgroup")
    if type(maximum) is not int or maximum <= 0:
        reasons.append("field_limits_unverified")
    if scene.creative_limit <= 0 or scene.copy_length_limit <= 0:
        reasons.append("field_limits_unverified")
    budget_key = (
        "adgroup_daily_budget"
        if config.budget_strategy == "ADGROUP"
        else "campaign_daily_budget"
    )
    if config.budget_strategy == "ADGROUP" and not isinstance(
        c.get("adgroup_daily_budget"), dict
    ):
        reasons.append("adgroup_budget_unverified")
    try:
        budget = c[budget_key]
        precision = Decimal(budget["precision"])
        if precision <= 0 or budget["currency"] != currency:
            reasons.append("budget_limits_unverified")
        elif (
            not (
                Decimal(budget["minimum_inclusive"])
                <= config.budget
                < Decimal(budget["maximum_exclusive"])
            )
            or config.budget % precision
        ):
            reasons.append("budget_out_of_range")
    except KeyError, TypeError, ValueError, InvalidOperation:
        reasons.append(
            "adgroup_budget_unverified"
            if config.budget_strategy == "ADGROUP"
            else "budget_limits_unverified"
        )
    # HIGHEST_VALUE 不发送 ROAS 出价；缺少 target_roas 不是场景核验失败。
    if config.bid_strategy == "TARGET_ROAS":
        try:
            if (
                not Decimal(c["roas_bid"]["minimum"])
                <= config.target_roas
                <= Decimal(c["roas_bid"]["maximum"])
            ):
                reasons.append("roas_out_of_range")
        except KeyError, TypeError, ValueError, InvalidOperation:
            reasons.append("roas_limits_unverified")
    capabilities = c.get("bid_capabilities")
    if isinstance(capabilities, dict):
        capability = capabilities.get(config.bid_strategy)
        if not isinstance(capability, dict):
            reasons.append("bid_strategy_invalid")
        else:
            expected = {
                "HIGHEST_VALUE": {
                    "optimization_goal": "VALUE",
                    "optimization_event": "AD_REVENUE_VALUE",
                    "deep_bid_type": "VO_HIGHEST_VALUE",
                },
                "TARGET_ROAS": {"deep_bid_type": "VO_MIN_ROAS"},
            }[config.bid_strategy]
            if any(capability.get(key) != value for key, value in expected.items()):
                reasons.append("bid_strategy_invalid")
    assets = scene.cta_fields.get("asset_ids", ())
    if not assets or any(x not in assets for x in config.cta_option_ids):
        reasons.append("cta_options_unavailable")
    return list(dict.fromkeys(reasons))
