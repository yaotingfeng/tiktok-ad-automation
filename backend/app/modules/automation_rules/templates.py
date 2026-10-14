"""规则模板：预填到编辑器的常用规则，阈值为示例值，由业务方按实际情况修改。

模板一律默认「仅通知」：用户确认阈值后再改为确认后执行或自动执行。
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuleTemplate:
    key: str
    name: str
    description: str
    definition: dict[str, Any]


def _cond(
    metric: str, operator: str, value: str, range_: str | None = None
) -> dict[str, Any]:
    condition: dict[str, Any] = {
        "metric": metric,
        "operator": operator,
        "values": [value],
    }
    if range_:
        condition["range"] = range_
    return condition


def _definition(
    *,
    level: str,
    aggregation: str,
    conditions: list[dict[str, Any]],
    action: dict[str, Any],
    frequency: str = "30M",
) -> dict[str, Any]:
    return {
        "accounts": {"mode": "ALL"},
        "apply_to": {"level": level, "status": "ACTIVE"},
        "aggregation": aggregation,
        "conditions": conditions,
        "action": action,
        "mode": "NOTIFY",
        "schedule": {"frequency": frequency, "window": "CONTINUOUS"},
        "notification": {"enabled": True},
    }


TEMPLATES: tuple[RuleTemplate, ...] = (
    RuleTemplate(
        "drama_stop_loss",
        "剧级止损",
        "每部剧今天合计消耗较高且 ROAS 偏低时，暂停该剧的广告组。",
        _definition(
            level="adgroup",
            aggregation="DRAMA",
            conditions=[
                _cond("spend", "GT", "100", "TODAY"),
                _cond("roas_total", "LT", "0.8", "TODAY"),
            ],
            action={"type": "PAUSE", "level": "adgroup"},
        ),
    ),
    RuleTemplate(
        "adgroup_stop_loss",
        "低效广告组止损",
        "广告组近 3 天消耗达到门槛且 ROAS 偏低时暂停。",
        _definition(
            level="adgroup",
            aggregation="EACH",
            conditions=[
                _cond("spend", "GT", "50", "PAST_3_DAYS"),
                _cond("roas_total", "LT", "0.8", "PAST_3_DAYS"),
            ],
            action={"type": "PAUSE", "level": "adgroup"},
        ),
    ),
    RuleTemplate(
        "scale_budget",
        "跑量加预算",
        "系列今天 ROAS 较高时预算提高 20%，设上限，每 24 小时最多一次。",
        _definition(
            level="campaign",
            aggregation="EACH",
            conditions=[_cond("roas_total", "GT", "1.5", "TODAY")],
            action={
                "type": "BUDGET",
                "level": "campaign",
                "adjustment": {
                    "mode": "INCREASE",
                    "value_type": "PERCENT",
                    "value": "20",
                    "limit": "1000",
                },
                "frequency": {"type": "EVERY_24H"},
            },
        ),
    ),
    RuleTemplate(
        "material_off",
        "低效素材下线",
        "素材近 3 天消耗达到门槛且 ROAS 偏低时暂停素材（素材报表每小时同步）。",
        _definition(
            level="material",
            aggregation="EACH",
            conditions=[
                _cond("spend", "GT", "30", "PAST_3_DAYS"),
                _cond("roas_total", "LT", "0.6", "PAST_3_DAYS"),
            ],
            action={"type": "PAUSE", "level": "material"},
            frequency="1H",
        ),
    ),
    RuleTemplate(
        "spend_breaker",
        "总花费熔断",
        "所有对象今天合计消耗超过上限时通知（可改为暂停系列）。",
        _definition(
            level="campaign",
            aggregation="ALL",
            conditions=[_cond("spend", "GT", "5000", "TODAY")],
            action={"type": "PAUSE", "level": "campaign"},
        ),
    ),
    RuleTemplate(
        "zero_spend",
        "零消耗预警",
        "创建超过 2 天、近 3 天没有消耗的广告组。",
        _definition(
            level="adgroup",
            aggregation="EACH",
            conditions=[
                _cond("days_since_creation", "GT", "2"),
                _cond("spend", "EQ", "0", "PAST_3_DAYS"),
            ],
            action={"type": "PAUSE", "level": "adgroup"},
        ),
    ),
)
