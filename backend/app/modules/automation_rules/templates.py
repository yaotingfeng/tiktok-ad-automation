"""规则模板：按投放生命周期（冷启动 → 测试 → 放量 → 衰退 → 风控）预置的推荐规则。

模板是给不熟悉自动规则的优化师直接选用的推荐配置，阈值为示例值（金额为账户币种），
用户按自己的预算规模微调即可。设计口径：

- 收益统一看第 0 天 ROAS（roas_d0）：短剧目标 ROAS 出价与出价保障（赔付）都按第 0 天核算，
  累计 ROAS 会随回收继续增长，不同日期成熟度不一致，不适合做止损判断。
- 关停、降预算类规则每天固定时刻用「昨天」等已结束日期判断：当天收入仍在回传，
  盘中 ROAS 普遍偏低；且出价保障按「系列 × 自然日」判定，当天暂停或降预算会失去当日赔付资格。
- 单对象规则按账户时区运行：账户时区各不相同（UTC、UTC-8、UTC+8 等），每个账户在当地
  时刻判断当地的「昨天」「今天」，数据成熟度一致；跨账户合计的剧目规则只能按规则时区运行。
- 判断前设「创建满 2 天」或消耗门槛，避免学习期样本不足就被误关。
- 加预算类用「对象自身预算 > 0」区分预算持有者：系列预算（CBO）只在系列上加，
  组预算只在广告组上加，避免广告组规则命中后把整个 CBO 系列的预算一起抬高。
- 模板一律默认「仅通知」：用户确认阈值后再改为确认后执行或自动执行。
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
    metric: str, operator: str, *values: str, range_: str | None = None
) -> dict[str, Any]:
    condition: dict[str, Any] = {
        "metric": metric,
        "operator": operator,
        "values": list(values),
    }
    if range_:
        condition["range"] = range_
    return condition


# 关停、降预算、降目标：每个账户当地 10:00 用当地昨天的完整数据判断一次。
_DAILY_REVIEW = {
    "frequency": "DAILY",
    "daily_at": "10:00",
    "window": "CONTINUOUS",
    "timezone_basis": "ACCOUNT",
}
# 跨账户合计需要同一时刻取数，按规则时区运行。默认北京时间 18:00（UTC 10:00），
# 此时 UTC-8 至 UTC+8 的账户「昨天」都已结束。
_CROSS_ACCOUNT_REVIEW = {
    "frequency": "DAILY",
    "daily_at": "18:00",
    "window": "CONTINUOUS",
}
# 加预算：账户当地白天每 2 小时检查；晚间加预算剩余时间太短，不在窗口内。
_DAYTIME_SCALE = {
    "frequency": "2H",
    "window": "CUSTOM",
    "periods": [{"date_type": "DAY", "start": "08:00", "end": "18:00"}],
    "timezone_basis": "ACCOUNT",
}
# 盘中风控：每 2 小时检查，持续命中时最多每 2 小时提醒一次。
_INTRADAY_GUARD = {"frequency": "2H", "window": "CONTINUOUS"}


def _definition(
    *,
    level: str,
    aggregation: str,
    conditions: list[dict[str, Any]],
    action: dict[str, Any],
    schedule: dict[str, Any],
) -> dict[str, Any]:
    return {
        "accounts": {"mode": "ALL"},
        "apply_to": {"level": level, "status": "ACTIVE"},
        "aggregation": aggregation,
        "conditions": conditions,
        "action": action,
        "mode": "NOTIFY",
        "schedule": schedule,
        "notification": {"enabled": True},
    }


def _budget_action(level: str, mode: str, percent: str, limit: str) -> dict[str, Any]:
    """按百分比调预算：提高必须给上限、降低必须给下限，同一对象每 24 小时最多调一次。"""
    return {
        "type": "BUDGET",
        "level": level,
        "adjustment": {
            "mode": mode,
            "value_type": "PERCENT",
            "value": percent,
            "limit": limit,
        },
        "frequency": {"type": "EVERY_24H"},
    }


TEMPLATES: tuple[RuleTemplate, ...] = (
    # 冷启动：新建对象两三天花不出钱，多半是审核、素材、出价过高或余额问题，需要人工排查。
    RuleTemplate(
        "cold_start_no_delivery",
        "冷启动 · 新建不起量预警",
        "创建满 2 天、近 3 天消耗不足 5 的系列。常见原因：素材拒审、目标 ROAS 过高、"
        "预算或余额不足。建议保持仅通知，人工排查后再处理。",
        _definition(
            level="campaign",
            aggregation="EACH",
            conditions=[
                _cond("days_since_creation", "GT", "1"),
                _cond("spend", "LT", "5", range_="PAST_3_DAYS"),
            ],
            action={"type": "PAUSE", "level": "campaign"},
            schedule=_DAILY_REVIEW,
        ),
    ),
    # 测试期：小预算广告组（如每组日预算 20）花够约 2 天预算仍明显不回本才关，
    # 同时要求昨天也差，避免一天波动就误关。
    RuleTemplate(
        "test_adgroup_stop_loss",
        "测试期 · 广告组止损",
        "创建满 2 天，近 3 天消耗超过 40、第 0 天 ROAS 低于 0.6，且昨天仍低于 0.7 时"
        "暂停广告组。消耗门槛建议设为组日预算的 2 倍左右。",
        _definition(
            level="adgroup",
            aggregation="EACH",
            conditions=[
                _cond("days_since_creation", "GT", "1"),
                _cond("spend", "GT", "40", range_="PAST_3_DAYS"),
                _cond("roas_d0", "LT", "0.6", range_="PAST_3_DAYS"),
                _cond("roas_d0", "LT", "0.7", range_="YESTERDAY"),
            ],
            action={"type": "PAUSE", "level": "adgroup"},
            schedule=_DAILY_REVIEW,
        ),
    ),
    # 测试期：素材没有创建时间，以近 7 天消耗作为样本量门槛；昨天仍在花且仍差才关。
    RuleTemplate(
        "test_material_cull",
        "测试期 · 低效素材淘汰",
        "素材近 7 天消耗超过 20、第 0 天 ROAS 低于 0.5，且昨天仍低于 0.6 时暂停该素材，"
        "把预算让给更好的素材。",
        _definition(
            level="material",
            aggregation="EACH",
            conditions=[
                _cond("spend", "GT", "20", range_="PAST_7_DAYS"),
                _cond("roas_d0", "LT", "0.5", range_="PAST_7_DAYS"),
                _cond("roas_d0", "LT", "0.6", range_="YESTERDAY"),
            ],
            action={"type": "PAUSE", "level": "material"},
            schedule=_DAILY_REVIEW,
        ),
    ),
    # 测试期：按剧合计所有账户判断这部剧是否值得继续测。影响面大，默认只通知。
    RuleTemplate(
        "drama_test_fail",
        "测试期 · 剧目测试不过线",
        "按剧合计所有账户：近 3 天消耗超过 300、第 0 天 ROAS 低于 0.7，且昨天仍低于 0.7 时"
        "提示该剧测试不过线。会暂停整部剧的广告组，建议仅通知、人工确认后再关。"
        "系列名须符合命名规范才能识别剧名。跨账户合计按规则时区每天 18:00 判断"
        "（默认北京时间，各时区账户的昨天均已结束）。",
        _definition(
            level="adgroup",
            aggregation="DRAMA",
            conditions=[
                _cond("spend", "GT", "300", range_="PAST_3_DAYS"),
                _cond("roas_d0", "LT", "0.7", range_="PAST_3_DAYS"),
                _cond("roas_d0", "LT", "0.7", range_="YESTERDAY"),
            ],
            action={"type": "PAUSE", "level": "adgroup"},
            schedule=_CROSS_ACCOUNT_REVIEW,
        ),
    ),
    # 放量（组预算）：昨天基本花满预算且回本好，今天开局仍在回本才加，每天最多一次。
    RuleTemplate(
        "scale_adgroup_budget",
        "放量 · 优质广告组加预算（组预算）",
        "仅限广告组自带预算（非 CBO）：昨天消耗超过 15、第 0 天 ROAS 高于 1.2，今天已消耗"
        "超过 5 且 ROAS 高于 1.1 时，预算提高 30%，最高 200，每天最多一次。"
        "昨天消耗门槛建议设为组日预算的 75%。",
        _definition(
            level="adgroup",
            aggregation="EACH",
            conditions=[
                _cond("budget", "GT", "0"),
                _cond("spend", "GT", "15", range_="YESTERDAY"),
                _cond("roas_d0", "GT", "1.2", range_="YESTERDAY"),
                _cond("spend", "GT", "5", range_="TODAY"),
                _cond("roas_d0", "GT", "1.1", range_="TODAY"),
            ],
            action=_budget_action("adgroup", "INCREASE", "30", "200"),
            schedule=_DAYTIME_SCALE,
        ),
    ),
    # 放量（CBO）：同上，只作用于持有预算的系列；单次涨幅 20%，避免打断学习。
    RuleTemplate(
        "scale_campaign_budget",
        "放量 · 优质系列加预算（CBO）",
        "仅限系列预算（CBO）：昨天消耗超过 300、第 0 天 ROAS 高于 1.15，今天已消耗超过 50"
        " 且 ROAS 高于 1.1 时，预算提高 20%，最高 10000，每天最多一次。"
        "昨天消耗门槛建议设为系列日预算的 70%~80%。",
        _definition(
            level="campaign",
            aggregation="EACH",
            conditions=[
                _cond("budget", "GT", "0"),
                _cond("spend", "GT", "300", range_="YESTERDAY"),
                _cond("roas_d0", "GT", "1.15", range_="YESTERDAY"),
                _cond("spend", "GT", "50", range_="TODAY"),
                _cond("roas_d0", "GT", "1.1", range_="TODAY"),
            ],
            action=_budget_action("campaign", "INCREASE", "20", "10000"),
            schedule=_DAYTIME_SCALE,
        ),
    ),
    # 放量：实际 ROAS 长期远高于目标却花不动，说明目标 ROAS 设得太保守，小步下调换量。
    RuleTemplate(
        "relax_roas_target",
        "放量 · 高 ROAS 跑不动降目标",
        "创建满 3 天，昨天消耗在 10~100 之间、第 0 天 ROAS 高于 1.3，且近 3 天仍高于 1.25 时，"
        "目标 ROAS 下调 0.05，最低 1.0，每 48 小时最多一次。注意：频繁改出价会重新学习，"
        "也可能影响出价保障资格。",
        _definition(
            level="campaign",
            aggregation="EACH",
            conditions=[
                _cond("days_since_creation", "GT", "2"),
                _cond("spend", "BETWEEN", "10", "100", range_="YESTERDAY"),
                _cond("roas_d0", "GT", "1.3", range_="YESTERDAY"),
                _cond("roas_d0", "GT", "1.25", range_="PAST_3_DAYS"),
            ],
            action={
                "type": "ROAS",
                "level": "campaign",
                "adjustment": {
                    "mode": "DECREASE",
                    "value_type": "AMOUNT",
                    "value": "0.05",
                    "limit": "1.0",
                },
                "frequency": {"type": "EVERY_48H"},
            },
            schedule=_DAILY_REVIEW,
        ),
    ),
    # 衰退：放量后的大系列一天变差不直接关，先降预算止血，保留模型积累。
    RuleTemplate(
        "decline_budget_trim",
        "衰退 · 效果回落降预算（CBO）",
        "仅限系列预算（CBO）：昨天消耗超过 300、第 0 天 ROAS 低于 0.85，且近 3 天低于 0.9 时，"
        "预算降低 20%，最低 200，每天最多一次。比直接暂停更适合已经跑量的系列。",
        _definition(
            level="campaign",
            aggregation="EACH",
            conditions=[
                _cond("budget", "GT", "0"),
                _cond("spend", "GT", "300", range_="YESTERDAY"),
                _cond("roas_d0", "LT", "0.85", range_="YESTERDAY"),
                _cond("roas_d0", "LT", "0.9", range_="PAST_3_DAYS"),
            ],
            action=_budget_action("campaign", "DECREASE", "20", "200"),
            schedule=_DAILY_REVIEW,
        ),
    ),
    # 风控：盘中只抓明显异常（链接失效、出价填错等），阈值必须放宽，正常回传延迟不应触发。
    RuleTemplate(
        "intraday_spend_anomaly",
        "风控 · 单系列盘中异常消耗",
        "系列今天消耗超过 500 且第 0 天 ROAS 低于 0.4 时提醒，用于发现链接失效、出价设置错误"
        "等明显异常。当天收入仍在回传，盘中 ROAS 偏低是常态，不建议改为自动暂停。",
        _definition(
            level="campaign",
            aggregation="EACH",
            conditions=[
                _cond("spend", "GT", "500", range_="TODAY"),
                _cond("roas_d0", "LT", "0.4", range_="TODAY"),
            ],
            action={"type": "PAUSE", "level": "campaign"},
            schedule=_INTRADAY_GUARD,
        ),
    ),
    # 风控：全部账户当天合计消耗超过日计划时提醒，用于控制现金流；按币种分别合计。
    RuleTemplate(
        "daily_spend_cap",
        "风控 · 全局日消耗超计划",
        "所选账户今天合计消耗超过日计划（示例 20000）时提醒。阈值按公司当日投放计划设置；"
        "命中后会暂停全部系列，务必保持仅通知。",
        _definition(
            level="campaign",
            aggregation="ALL",
            conditions=[_cond("spend", "GT", "20000", range_="TODAY")],
            action={"type": "PAUSE", "level": "campaign"},
            schedule=_INTRADAY_GUARD,
        ),
    ),
)
