import pytest
from pydantic import ValidationError

from app.modules.automation_rules.catalog import (
    RULE_METRICS,
    allowed_action_levels,
)
from app.modules.automation_rules.schemas import RuleDefinition


def base(**overrides):
    value = {
        "accounts": {"mode": "ALL"},
        "apply_to": {"level": "adgroup", "status": "ACTIVE"},
        "aggregation": "EACH",
        "conditions": [
            {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["10"]}
        ],
        "action": {"type": "PAUSE", "level": "adgroup"},
        "mode": "NOTIFY",
        "schedule": {"frequency": "30M", "window": "CONTINUOUS"},
    }
    value.update(overrides)
    return value


def test_valid_definition_defaults():
    rule = RuleDefinition.model_validate(base())
    assert rule.notification.enabled is True


@pytest.mark.parametrize(
    "patch",
    [
        {"max_targets": 200},
        {"apply_to": {"level": "adgroup", "status": "ACTIVE", "drama_names": ["x"]}},
        {"apply_to": {"level": "adgroup", "status": "ACTIVE", "name_contains": "x"}},
        {"apply_to": {"level": "adgroup", "status": "ACTIVE", "name_excludes": "x"}},
    ],
)
def test_removed_scope_filters_and_target_limit_rejected(patch):
    # 名称过滤用条件里的「名称」，不再有单独的范围过滤框；不再有单次执行对象上限。
    with pytest.raises(ValidationError):
        RuleDefinition.model_validate(base(**patch))


def test_notify_only_rule_must_send_notification():
    with pytest.raises(ValidationError):
        RuleDefinition.model_validate(base(notification={"enabled": False}))
    RuleDefinition.model_validate(base(mode="CONFIRM", notification={"enabled": False}))


def test_material_level_lacks_day2_revenue():
    assert "material" not in RULE_METRICS["roas_d3"].levels
    assert "material" in RULE_METRICS["roas_d7"].levels


@pytest.mark.parametrize(
    "patch",
    [
        {"conditions": []},
        {"conditions": [base()["conditions"][0]] * 6},
        {
            "conditions": [
                {"metric": "nope", "range": "TODAY", "operator": "GT", "values": ["1"]}
            ]
        },
        {
            "conditions": [
                {
                    "metric": "spend",
                    "range": "TODAY",
                    "operator": "BETWEEN",
                    "values": ["1"],
                }
            ]
        },
        {
            "conditions": [
                {
                    "metric": "spend",
                    "range": "TODAY",
                    "operator": "BETWEEN",
                    "values": ["5", "1"],
                }
            ]
        },
        {"conditions": [{"metric": "spend", "operator": "GT", "values": ["1"]}]},
        {"conditions": [{"metric": "name", "operator": "GT", "values": ["1"]}]},
        {
            "aggregation": "DRAMA",
            "conditions": [{"metric": "budget", "operator": "GT", "values": ["1"]}],
        },
        {"apply_to": {"level": "material", "status": "ACTIVE"}, "aggregation": "DRAMA"},
        # 视频口径只用于素材层级，且只能对素材本身执行。
        {"aggregation": "VIDEO"},
        {
            "apply_to": {"level": "material", "status": "ACTIVE"},
            "aggregation": "VIDEO",
            "action": {"type": "PAUSE", "level": "ad"},
            "schedule": {"frequency": "1H", "window": "CONTINUOUS"},
        },
        # 跨账户合计需同一时刻取数，不能按账户时区运行。
        {
            "apply_to": {"level": "material", "status": "ACTIVE"},
            "aggregation": "VIDEO",
            "action": {"type": "PAUSE", "level": "material"},
            "schedule": {
                "frequency": "1H",
                "window": "CONTINUOUS",
                "timezone_basis": "ACCOUNT",
            },
        },
        {
            "action": {
                "type": "BUDGET",
                "level": "ad",
                "adjustment": {"mode": "SET", "value_type": "AMOUNT", "value": "50"},
            }
        },
        {"action": {"type": "PAUSE", "level": "campaign"}},
        {
            "action": {
                "type": "BUDGET",
                "level": "adgroup",
                "adjustment": {
                    "mode": "INCREASE",
                    "value_type": "PERCENT",
                    "value": "20",
                },
            }
        },
        {
            "apply_to": {"level": "material", "status": "ACTIVE"},
            "action": {"type": "PAUSE", "level": "material"},
        },
        {"apply_to": {"level": "adgroup", "status": "SELECTED"}},
        {"accounts": {"mode": "BC"}},
        {"schedule": {"frequency": "DAILY", "window": "CONTINUOUS"}},
        {"schedule": {"frequency": "30M", "window": "ONCE"}},
        # 「仅一次」是绝对时刻，合计口径需要同一时刻取数，都不能按账户时区运行。
        {
            "schedule": {
                "frequency": "30M",
                "window": "ONCE",
                "once_at": "2026-10-20T00:00:00+00:00",
                "timezone_basis": "ACCOUNT",
            }
        },
        {
            "aggregation": "DRAMA",
            "schedule": {
                "frequency": "30M",
                "window": "CONTINUOUS",
                "timezone_basis": "ACCOUNT",
            },
        },
        {
            "schedule": {
                "frequency": "30M",
                "window": "CUSTOM",
                "periods": [
                    {"date_type": "WEEK", "days": [], "start": "09:00", "end": "18:00"}
                ],
            }
        },
    ],
)
def test_invalid_definitions(patch):
    with pytest.raises(ValidationError):
        RuleDefinition.model_validate(base(**patch))


def test_action_levels_follow_judge_scope():
    assert allowed_action_levels("EACH", "adgroup", "PAUSE") == (
        "adgroup",
        "ad",
        "material",
    )
    assert allowed_action_levels("DRAMA", "adgroup", "PAUSE") == (
        "campaign",
        "adgroup",
        "ad",
        "material",
    )
    assert allowed_action_levels("EACH", "ad", "BUDGET") == ()
    assert allowed_action_levels("EACH", "campaign", "ROAS") == ("campaign",)


def test_video_total_is_a_material_scope():
    rule = RuleDefinition.model_validate(
        base(
            apply_to={"level": "material", "status": "ACTIVE"},
            aggregation="VIDEO",
            action={"type": "PAUSE", "level": "material"},
            schedule={"frequency": "1H", "window": "CONTINUOUS"},
        )
    )
    assert rule.aggregation == "VIDEO"
    assert allowed_action_levels("VIDEO", "material", "PAUSE") == ("material",)
    assert "VIDEO" in RULE_METRICS["spend"].aggregations
    assert "VIDEO" not in RULE_METRICS["roas_d3"].aggregations
