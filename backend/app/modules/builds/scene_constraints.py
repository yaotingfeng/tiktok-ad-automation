"""Versioned platform facts and separately identified application copy policy.

An undocumented platform maximum stays None. This application independently
limits its English copy pool to 1..100 characters; it is not a TikTok quota.
"""

from typing import Any

REVISION = "minis-constraints-2026-09-09-v2"
PLATFORM_COPY_LENGTH_LIMIT: int | None = None
COPY_LENGTH_LIMIT = 100
COPY_LENGTH_MEASUREMENT = "characters"


def constraints_for(currency: str) -> tuple[dict[str, Any], tuple[str, ...]]:
    reasons: list[str] = []
    constraints: dict[str, Any] = {
        "revision": REVISION,
        "name_limits": {"campaign": 512, "adgroup": 512, "ad": 512},
        "name_measurement": {
            "campaign": "cjk_weighted",
            "adgroup": "characters",
            "ad": "cjk_weighted",
        },
        "emoji_allowed": False,
        "max_creatives_per_ad": 50,
        "max_ads_per_adgroup": 30,
        "roas_bid": {"minimum": "0.01", "maximum": "1000"},
        "platform_copy_length": PLATFORM_COPY_LENGTH_LIMIT,
        "copy_policy": {
            "source": "APPLICATION",
            "minimum": 1,
            "maximum": COPY_LENGTH_LIMIT,
            "measurement": COPY_LENGTH_MEASUREMENT,
        },
        "copy_length": COPY_LENGTH_LIMIT,
        "copy_measurement": COPY_LENGTH_MEASUREMENT,
    }
    # This narrowly verified currency is explicit, not inherited from account defaults.
    if currency == "USD":
        constraints["campaign_daily_budget"] = {
            "currency": "USD",
            "minimum_inclusive": "50",
            "maximum_exclusive": "10000000",
            "precision": "0.01",
        }
    else:
        reasons.append("budget_limits_unverified")
    return constraints, tuple(reasons)
