from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.strategies.schemas import StrategyConfig


def base_config(**overrides):
    return StrategyConfig.model_validate(
        {
            "budget": "100.25",
            "currency": "USD",
            "copy_pool_version": uuid4(),
            **overrides,
        }
    )


def test_defaults_describe_one_series_one_group_and_one_material_per_ad():
    config = base_config()

    assert config.budget_strategy == "SERIES"
    assert config.bid_strategy == "HIGHEST_VALUE"
    assert config.group_generation_mode == "FIXED"
    assert config.group_count == 1
    assert config.group_material_allocation == "SHARED"
    assert config.max_materials_per_group is None
    assert config.ad_generation_mode == "BY_MATERIAL"
    assert config.ads_per_group is None
    assert config.ad_material_allocation is None
    assert config.max_materials_per_ad == 1
    assert config.creative_count == 1
    assert config.target_roas is None
    assert "series_count" not in config.model_dump()


@pytest.mark.parametrize(
    "changes",
    [
        {"group_generation_mode": "FIXED", "group_count": None},
        {
            "group_generation_mode": "BY_MATERIAL",
            "group_count": 1,
            "max_materials_per_group": None,
        },
        {"ad_generation_mode": "FIXED", "ads_per_group": None},
        {
            "ad_generation_mode": "BY_MATERIAL",
            "ads_per_group": 1,
            "max_materials_per_ad": None,
        },
    ],
)
def test_generation_mode_requires_its_matching_quantity(changes):
    with pytest.raises(ValidationError):
        base_config(**changes)


@pytest.mark.parametrize("field", ["group_count", "ads_per_group"])
def test_fixed_structure_counts_have_a_bounded_product_limit(field):
    with pytest.raises(ValidationError) as error:
        base_config(
            group_count=101 if field == "group_count" else 1,
            ad_generation_mode="FIXED",
            ads_per_group=101 if field == "ads_per_group" else 1,
            max_materials_per_ad=None,
        )

    assert field in str(error.value)


def test_legacy_fixed_count_can_be_read_without_truncation():
    from app.modules.strategies.saved_config import read_saved_config

    saved = base_config().model_dump(mode="json") | {
        "group_size": 10,
        "creative_count": 101,
    }

    current = read_saved_config(saved)

    assert current.ads_per_group == 101


def test_allocation_is_only_valid_for_fixed_generation():
    with pytest.raises(ValidationError):
        base_config(
            group_generation_mode="BY_MATERIAL",
            group_count=None,
            group_material_allocation="SEQUENTIAL_AVERAGE",
            max_materials_per_group=10,
        )
    with pytest.raises(ValidationError):
        base_config(
            ad_generation_mode="BY_MATERIAL",
            ads_per_group=None,
            ad_material_allocation="SHARED",
            max_materials_per_ad=2,
        )


def test_bid_strategy_controls_target_roas_presence():
    highest = base_config()
    assert highest.target_roas is None
    target = base_config(bid_strategy="TARGET_ROAS", target_roas="1.08")
    assert target.target_roas == Decimal("1.08")

    with pytest.raises(ValidationError):
        base_config(target_roas="1.08")
    with pytest.raises(ValidationError):
        base_config(bid_strategy="TARGET_ROAS")


@pytest.mark.parametrize("field", ["group_size", "account_pool"])
def test_removed_and_unknown_fields_are_rejected(field):
    with pytest.raises(ValidationError):
        base_config(**{field: 10 if field == "group_size" else []})
