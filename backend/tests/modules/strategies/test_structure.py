from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.core.errors import DomainError
from app.modules.strategies.copy_pool import seed_copies
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.structure import plan_structure


def base_config(**overrides) -> StrategyConfig:
    return StrategyConfig.model_validate(
        {
            "budget": "100.00",
            "currency": "USD",
            "copy_pool_version": uuid4(),
            **overrides,
        }
    )


def materials(count: int, *, names: tuple[str, ...] | None = None):
    return [
        SimpleNamespace(
            material_id=UUID(int=index + 1),
            file_name=(names[index] if names else f"Drama-{index:02}.mp4"),
        )
        for index in range(count)
    ]


def test_fixed_group_allocation_shared_and_average():
    shared = plan_structure(
        materials(20),
        config=base_config(
            group_count=2,
            group_material_allocation="SHARED",
            ad_generation_mode="FIXED",
            ads_per_group=1,
            max_materials_per_ad=None,
            ad_material_allocation="SHARED",
        ),
        pool=seed_copies(),
        seed=7,
    )
    average = plan_structure(
        materials(20),
        config=base_config(
            group_count=2,
            group_material_allocation="SEQUENTIAL_AVERAGE",
            ad_generation_mode="FIXED",
            ads_per_group=1,
            max_materials_per_ad=None,
            ad_material_allocation="SHARED",
        ),
        pool=seed_copies(),
        seed=7,
    )
    uneven = plan_structure(
        materials(25),
        config=base_config(
            group_count=2,
            group_material_allocation="SEQUENTIAL_AVERAGE",
            ad_generation_mode="FIXED",
            ads_per_group=1,
            max_materials_per_ad=None,
            ad_material_allocation="SHARED",
        ),
        pool=seed_copies(),
        seed=7,
    )

    assert tuple(len(group.material_ids) for group in shared) == (20, 20)
    assert tuple(len(group.material_ids) for group in average) == (10, 10)
    assert tuple(len(group.material_ids) for group in uneven) == (13, 12)


def test_material_bounded_groups_split_in_order():
    groups = plan_structure(
        materials(25),
        config=base_config(
            group_generation_mode="BY_MATERIAL",
            group_count=None,
            group_material_allocation=None,
            max_materials_per_group=10,
            ad_generation_mode="FIXED",
            ads_per_group=1,
            max_materials_per_ad=None,
            ad_material_allocation="SHARED",
        ),
        pool=seed_copies(),
        seed=7,
    )

    assert tuple(len(group.material_ids) for group in groups) == (10, 10, 5)


@pytest.mark.parametrize("allocation", ["SHARED", "SEQUENTIAL_AVERAGE"])
def test_fixed_ad_allocation(allocation):
    group = plan_structure(
        materials(10),
        config=base_config(
            group_count=1,
            group_material_allocation="SHARED",
            ad_generation_mode="FIXED",
            ads_per_group=2,
            max_materials_per_ad=None,
            ad_material_allocation=allocation,
        ),
        pool=seed_copies(),
        seed=7,
    )[0]

    assert tuple(len(ad.material_ids) for ad in group.ads) == (
        (10, 10) if allocation == "SHARED" else (5, 5)
    )


@pytest.mark.parametrize(
    ("max_materials_per_ad", "expected_ads"), [(1, 20), (10, 2)]
)
def test_material_bounded_ads(max_materials_per_ad, expected_ads):
    group = plan_structure(
        materials(20),
        config=base_config(
            group_count=1,
            group_material_allocation="SHARED",
            ad_generation_mode="BY_MATERIAL",
            ads_per_group=None,
            ad_material_allocation=None,
            max_materials_per_ad=max_materials_per_ad,
        ),
        pool=seed_copies(),
        seed=7,
    )[0]

    assert len(group.ads) == expected_ads
    assert all(len(ad.material_ids) <= max_materials_per_ad for ad in group.ads)


def test_creative_count_copies_each_ad_material_set():
    group = plan_structure(
        materials(10),
        config=base_config(
            group_count=1,
            group_material_allocation="SHARED",
            ad_generation_mode="FIXED",
            ads_per_group=2,
            max_materials_per_ad=None,
            ad_material_allocation="SHARED",
            creative_count=3,
        ),
        pool=seed_copies(),
        seed=51,
    )[0]

    assert len(group.ads) == 6
    for base_ad_no in (1, 2):
        copies = [ad for ad in group.ads if ad.base_ad_no == base_ad_no]
        assert len(copies) == 3
        assert {ad.material_ids for ad in copies} == {
            tuple(UUID(int=i) for i in range(1, 11))
        }
        assert len({ad.copies[0].text for ad in copies}) == 3
        assert all(len(ad.copies) == 1 for ad in copies)


def test_material_ids_are_deduplicated_but_same_name_ids_remain_distinct():
    items = [
        SimpleNamespace(material_id=UUID(int=value), file_name="Moon.mp4")
        for value in (3, 1, 2, 1)
    ]
    group = plan_structure(
        items,
        config=base_config(
            group_count=1,
            group_material_allocation="SHARED",
            ad_generation_mode="FIXED",
            ads_per_group=1,
            max_materials_per_ad=None,
            ad_material_allocation="SHARED",
        ),
        pool=seed_copies(),
        seed=1,
    )[0]

    assert group.material_ids == (UUID(int=1), UUID(int=2), UUID(int=3))


@pytest.mark.parametrize(
    "config",
    [
        base_config(
            group_count=2,
            group_material_allocation="SEQUENTIAL_AVERAGE",
            ad_generation_mode="FIXED",
            ads_per_group=1,
            max_materials_per_ad=None,
            ad_material_allocation="SHARED",
        ),
        base_config(
            group_count=1,
            group_material_allocation="SHARED",
            ad_generation_mode="FIXED",
            ads_per_group=2,
            max_materials_per_ad=None,
            ad_material_allocation="SEQUENTIAL_AVERAGE",
        ),
    ],
)
def test_average_allocation_rejects_more_units_than_materials(config):
    with pytest.raises(DomainError, match="invalid_material_allocation"):
        plan_structure(materials(1), config=config, pool=seed_copies(), seed=7)
