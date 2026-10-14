from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.automation_rules.actions import (
    action_key,
    frequency_allows,
    group_by_bc,
    mutation_for,
    opposite,
    selection_payload,
)
from app.modules.automation_rules.schemas import Action, Frequency

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def act(type_, level="adgroup", **adjust):
    if type_ in ("PAUSE", "ENABLE"):
        return Action(type=type_, level=level)
    return Action(
        type=type_, level=level, adjustment=adjust, frequency={"type": "EVERY_24H"}
    )


@pytest.mark.parametrize(
    "action, key, field, mode, bounds",
    [
        (act("PAUSE"), "PAUSE", "status", "set", {}),
        (act("ENABLE"), "ENABLE", "status", "set", {}),
        (
            act(
                "BUDGET", mode="INCREASE", value_type="PERCENT", value="20", limit="100"
            ),
            "BUDGET_UP",
            "budget",
            "increase_percent",
            {"max_value": Decimal("100")},
        ),
        (
            act("BUDGET", mode="INCREASE", value_type="AMOUNT", value="5", limit="100"),
            "BUDGET_UP",
            "budget",
            "add",
            {"max_value": Decimal("100")},
        ),
        (
            act(
                "BUDGET", mode="DECREASE", value_type="PERCENT", value="20", limit="30"
            ),
            "BUDGET_DOWN",
            "budget",
            "decrease_percent",
            {"min_value": Decimal("30")},
        ),
        (
            act(
                "ROAS",
                level="campaign",
                mode="DECREASE",
                value_type="AMOUNT",
                value="0.1",
                limit="0.5",
            ),
            "ROAS_DOWN",
            "roas",
            "subtract",
            {"min_value": Decimal("0.5")},
        ),
        (
            act("ROAS", level="campaign", mode="SET", value_type="AMOUNT", value="1.2"),
            "ROAS_SET",
            "roas",
            "set",
            {},
        ),
    ],
)
def test_mapping(action, key, field, mode, bounds):
    assert action_key(action) == key
    spec = mutation_for(action)
    assert (spec.field, spec.mode) == (field, mode)
    for name, value in bounds.items():
        assert getattr(spec, name) == value


def test_pause_value_and_material_field():
    assert mutation_for(act("PAUSE")).value == "DISABLE"
    assert mutation_for(act("ENABLE")).value == "ENABLE"
    # 广告管理以 status + 素材使用表示素材启停。
    assert mutation_for(act("PAUSE", level="material")).field == "status"


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("PAUSE", "ENABLE", True),
        ("ENABLE", "PAUSE", True),
        ("PAUSE", "PAUSE", False),
        ("BUDGET_UP", "BUDGET_DOWN", True),
        ("BUDGET_UP", "BUDGET_UP", False),
        ("BUDGET_SET", "BUDGET_UP", True),
        ("BUDGET_SET", "BUDGET_SET", True),
        ("BUDGET_UP", "ROAS_DOWN", False),
        ("PAUSE", "BUDGET_UP", False),
    ],
)
def test_opposite(a, b, expected):
    assert opposite(a, b) is expected


def at(hours):
    return NOW - timedelta(hours=hours)


@pytest.mark.parametrize(
    "frequency, history, expected",
    [
        (None, [at(1)], True),
        ({"type": "ONCE"}, [], True),
        ({"type": "ONCE"}, [at(500)], False),
        ({"type": "EVERY_24H"}, [at(23)], False),
        ({"type": "EVERY_24H"}, [at(24)], True),
        ({"type": "EVERY_48H"}, [at(47)], False),
        ({"type": "EVERY_WEEK"}, [at(24 * 7)], True),
        (
            {"type": "CUSTOM", "window": 6, "unit": "HOUR", "count": 2},
            [at(1), at(5)],
            False,
        ),
        (
            {"type": "CUSTOM", "window": 6, "unit": "HOUR", "count": 2},
            [at(1), at(7)],
            True,
        ),
        ({"type": "CUSTOM", "window": 30, "unit": "MINUTE", "count": 1}, [at(1)], True),
    ],
)
def test_frequency(frequency, history, expected):
    freq = Frequency.model_validate(frequency) if frequency else None
    assert frequency_allows(freq, history, NOW) is expected


def test_group_and_selection_payload():
    tenant = uuid4()
    use = {
        "advertiser_id": "a2",
        "ad_remote_id": "x9",
        "platform_material_id": "v1",
        "ad_material_id": "m1",
        "material_type": "VIDEO",
    }
    targets = [
        {
            "key": "adgroup:a1:g1",
            "kind": "adgroup",
            "advertiser_id": "a1",
            "bc_id": "bc-2",
            "remote_id": "g1",
            "material_uses": [],
        },
        {
            "key": "material:a2:v1",
            "kind": "material",
            "advertiser_id": "a2",
            "bc_id": "bc-1",
            "remote_id": "v1",
            "material_uses": [use],
        },
    ]
    groups = group_by_bc(targets)
    assert list(groups) == ["bc-1", "bc-2"]
    refs, uses = selection_payload(groups["bc-2"], tenant)
    assert refs == [
        {
            "tenant_id": str(tenant),
            "advertiser_id": "a1",
            "kind": "adgroup",
            "remote_id": "g1",
        }
    ]
    refs, uses = selection_payload(groups["bc-1"], tenant)
    # 素材使用所在的广告也进入 refs，广告管理按 refs 加载目录对象。
    assert refs == [
        {
            "tenant_id": str(tenant),
            "advertiser_id": "a2",
            "kind": "ad",
            "remote_id": "x9",
        }
    ]
    assert uses == [
        {
            "ad_ref": {
                "tenant_id": str(tenant),
                "advertiser_id": "a2",
                "kind": "ad",
                "remote_id": "x9",
            },
            "platform_material_id": "v1",
            "ad_material_id": "m1",
            "material_type": "VIDEO",
        }
    ]
