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
    elif config.creative_count > maximum:
        reasons.append("creative_count_exceeded")
    if scene.creative_limit <= 0 or scene.copy_length_limit <= 0:
        reasons.append("field_limits_unverified")
    try:
        budget = c["campaign_daily_budget"]
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
        reasons.append("budget_limits_unverified")
    try:
        if (
            not Decimal(c["roas_bid"]["minimum"])
            <= config.target_roas
            <= Decimal(c["roas_bid"]["maximum"])
        ):
            reasons.append("roas_out_of_range")
    except KeyError, TypeError, ValueError, InvalidOperation:
        reasons.append("roas_limits_unverified")
    assets = scene.cta_fields.get("asset_ids", ())
    if not assets or any(x not in assets for x in config.cta_option_ids):
        reasons.append("cta_options_unavailable")
    return list(dict.fromkeys(reasons))
