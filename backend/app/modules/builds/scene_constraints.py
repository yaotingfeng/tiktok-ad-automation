"""Versioned platform facts and separately identified application copy policy.

An undocumented platform maximum stays None. This application independently
limits its English copy pool to 1..100 characters; it is not a TikTok quota.
"""

from typing import Any

REVISION = "minis-constraints-2026-10-04-v3"
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
        # None 表示账户级只读事实尚未核实，不能推断为平台不支持或自动降级。
        "adgroup_daily_budget": None,
        # 组预算能力必须有独立的账户级证据；默认不声明支持，预览会阻断。
        # 这里保留能力槽位而不伪造数值，scene job 可在取得只读证据后填充。
        "budget_capabilities": {
            "campaign_daily_budget": "verified",
            "adgroup_daily_budget": "unverified",
        },
        "bid_capabilities": {
            "HIGHEST_VALUE": {
                "optimization_goal": "VALUE",
                "optimization_event": "IMPRESSION_LEVEL_AD_REVENUE",
                "deep_bid_type": "VO_HIGHEST_VALUE",
            },
            "TARGET_ROAS": {"deep_bid_type": "VO_MIN_ROAS"},
        },
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
