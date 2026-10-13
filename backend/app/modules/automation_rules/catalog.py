"""自动规则可用的指标与层级规则。

指标只来自 TK-ADA 已同步的报表事实(报表指标字典)和对象目录属性;
不提供平台有、但我们没有同步的指标。
"""

from dataclasses import dataclass
from typing import Literal

from app.modules.reporting.metric_catalog import METRIC_CATALOG

Level = Literal["campaign", "adgroup", "ad", "material"]
Aggregation = Literal["EACH", "DRAMA", "ALL"]
ActionType = Literal["PAUSE", "ENABLE", "BUDGET", "ROAS"]
LEVELS: tuple[Level, ...] = ("campaign", "adgroup", "ad", "material")
NUMERIC_OPERATORS = ("GT", "LT", "BETWEEN", "EQ")
TEXT_OPERATORS = ("CONTAINS", "NOT_CONTAINS", "STARTS_WITH", "ENDS_WITH", "EQ")
RANGES = ("TODAY", "YESTERDAY", "PAST_3_DAYS", "PAST_5_DAYS", "PAST_7_DAYS", "LIFETIME")


@dataclass(frozen=True)
class RuleMetric:
    key: str
    group: Literal["delivery", "revenue", "attribute"]
    unit: str
    kind: Literal["fact", "attribute"]
    levels: frozenset[str]
    aggregations: frozenset[str]
    operators: tuple[str, ...]
    ranged: bool


_DELIVERY = {"spend", "impressions", "clicks", "ctr", "cpm", "cpc"}


def _fact_metrics() -> dict[str, RuleMetric]:
    result = {}
    for item in METRIC_CATALOG:
        levels = {"campaign", "adgroup", "ad"}
        # 素材报表缺少的天数(第 2、29 天)在素材层级不可选。
        if item.material_field is not None:
            levels.add("material")
        result[item.key] = RuleMetric(
            key=item.key,
            group="delivery" if item.key in _DELIVERY else "revenue",
            unit=item.unit,
            kind="fact",
            levels=frozenset(levels),
            aggregations=frozenset({"EACH", "DRAMA", "ALL"}),
            operators=NUMERIC_OPERATORS,
            ranged=True,
        )
    return result


def _attributes() -> dict[str, RuleMetric]:
    def attr(key, unit, levels, operators):
        # 属性描述单个对象,合计口径下没有意义,只允许「每个对象」。
        return RuleMetric(
            key,
            "attribute",
            unit,
            "attribute",
            frozenset(levels),
            frozenset({"EACH"}),
            operators,
            False,
        )

    return {
        "name": attr("name", "text", LEVELS, TEXT_OPERATORS),
        "days_since_creation": attr(
            "days_since_creation",
            "day",
            ("campaign", "adgroup", "ad"),
            NUMERIC_OPERATORS,
        ),
        "budget": attr("budget", "money", ("campaign", "adgroup"), NUMERIC_OPERATORS),
        "roas_bid": attr(
            "roas_bid", "multiple", ("campaign", "adgroup"), NUMERIC_OPERATORS
        ),
    }


RULE_METRICS: dict[str, RuleMetric] = {**_fact_metrics(), **_attributes()}

_DEPTH = {level: index for index, level in enumerate(LEVELS)}
# 各操作支持的执行层级;ROAS 以广告管理现有 roas 变更为准(系列)。
ACTION_LEVELS: dict[str, tuple[str, ...]] = {
    "PAUSE": LEVELS,
    "ENABLE": LEVELS,
    "BUDGET": ("campaign", "adgroup"),
    "ROAS": ("campaign",),
}


def allowed_action_levels(
    aggregation: str, apply_level: str, action_type: str
) -> tuple[str, ...]:
    """执行层级必须在判断对象范围内:合计口径不限,单对象口径只能是自身或下级。"""
    supported = ACTION_LEVELS[action_type]
    if aggregation != "EACH":
        return supported
    return tuple(level for level in supported if _DEPTH[level] >= _DEPTH[apply_level])


def min_frequency_minutes(apply_level: str, action_level: str) -> int:
    # 素材报表 60 分钟同步一次,更频繁的检查只会看到同一份数据。
    return 60 if "material" in (apply_level, action_level) else 30
