"""Reviewed platform constraint revision. No runtime/client limit overrides.

Update only with new official evidence and contract tests. Missing evidence is
represented by None, never by a fictional zero platform limit.
"""

from typing import Any

REVISION = "minis-constraints-2026-09-09-v1"
COPY_LENGTH_LIMIT: int | None = None
COPY_LENGTH_MEASUREMENT: str | None = None


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
    if COPY_LENGTH_LIMIT is None or COPY_LENGTH_MEASUREMENT is None:
        reasons.append("field_limits_unverified")
    return constraints, tuple(reasons)
