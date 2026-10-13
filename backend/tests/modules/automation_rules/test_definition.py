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
    assert rule.max_targets == 200
    assert rule.notification.enabled is True


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
