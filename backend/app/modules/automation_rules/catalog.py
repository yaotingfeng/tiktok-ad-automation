"""自动规则可用的指标与层级规则。

指标只来自 TK-ADA 已同步的报表事实(报表指标字典)和对象目录属性;
不提供平台有、但我们没有同步的指标。
"""

from dataclasses import dataclass
from typing import Literal

from app.modules.reporting.metric_catalog import METRIC_CATALOG

Level = Literal["campaign", "adgroup", "ad", "material"]
Aggregation = Literal["EACH", "DRAMA", "ALL", "VIDEO"]
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
    options: tuple[tuple[str, str], ...] = ()


_DELIVERY = {"spend", "impressions", "clicks", "ctr", "cpm", "cpc"}


def _fact_metrics() -> dict[str, RuleMetric]:
    result = {}
    for item in METRIC_CATALOG:
        levels = {"campaign", "adgroup", "ad"}
        aggregations = {"EACH", "DRAMA", "ALL"}
        # 素材报表缺少的天数(第 2、29 天)在素材层级不可选。
        if item.material_field is not None:
            levels.add("material")
            # 同一视频跨账户合计只有素材报表事实可用。
            aggregations.add("VIDEO")
        result[item.key] = RuleMetric(
            key=item.key,
            group="delivery" if item.key in _DELIVERY else "revenue",
            unit=item.unit,
            kind="fact",
            levels=frozenset(levels),
            aggregations=frozenset(aggregations),
            operators=NUMERIC_OPERATORS,
            ranged=True,
        )
    return result


def _attributes() -> dict[str, RuleMetric]:
    def attr(
        key: str, unit: str, levels: tuple[str, ...], operators: tuple[str, ...]
    ) -> RuleMetric:
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

    attributes = {
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
    # 状态是对象自身的操作状态；上级状态独立提供，避免把暂停继承混为一谈。
    for index, level in enumerate(LEVELS):
        attributes[f"{level}_status"] = RuleMetric(
            f"{level}_status",
            "attribute",
            "status",
            "attribute",
            frozenset(LEVELS[index:]),
            frozenset({"EACH"}),
            ("EQ",),
            False,
            (("ENABLE", "开启"), ("DISABLE", "暂停")),
        )
    return attributes


RULE_METRICS: dict[str, RuleMetric] = {**_fact_metrics(), **_attributes()}

_DEPTH: dict[str, int] = {level: index for index, level in enumerate(LEVELS)}
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
    # 视频口径的判断对象就是各账户里的这条素材，只能对素材本身执行。
    if aggregation == "VIDEO":
        return tuple(level for level in supported if level == "material")
    if aggregation != "EACH":
        return supported
    return tuple(level for level in supported if _DEPTH[level] >= _DEPTH[apply_level])


def min_frequency_minutes(apply_level: str, action_level: str, action_type: str) -> int:
    # 当前日报表每 2 分钟同步：仅暂停允许 5 分钟止损，恢复和金额调整保持原频率。
    if action_type == "PAUSE":
        return 5
    return 60 if "material" in (apply_level, action_level) else 30
