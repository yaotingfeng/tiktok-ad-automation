from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.core.errors import DomainError
from app.modules.strategies.copy_pool import CopyChoice, seed_copies
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
            unit_id=UUID(int=index + 1),
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

    assert tuple(len(group.unit_ids) for group in shared) == (20, 20)
    assert tuple(len(group.unit_ids) for group in average) == (10, 10)
    assert tuple(len(group.unit_ids) for group in uneven) == (13, 12)
    assert average[0].unit_ids == tuple(UUID(int=i) for i in range(1, 11))
    assert average[1].unit_ids == tuple(UUID(int=i) for i in range(11, 21))
    assert set(average[0].unit_ids).isdisjoint(average[1].unit_ids)


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

    assert tuple(len(group.unit_ids) for group in groups) == (10, 10, 5)


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

    assert tuple(len(ad.unit_ids) for ad in group.ads) == (
        (10, 10) if allocation == "SHARED" else (5, 5)
    )
    if allocation == "SEQUENTIAL_AVERAGE":
        assert group.ads[0].unit_ids == tuple(UUID(int=i) for i in range(1, 6))
        assert group.ads[1].unit_ids == tuple(UUID(int=i) for i in range(6, 11))
        assert set(group.ads[0].unit_ids).isdisjoint(group.ads[1].unit_ids)


@pytest.mark.parametrize(("max_materials_per_ad", "expected_ads"), [(1, 20), (10, 2)])
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
    assert all(len(ad.unit_ids) <= max_materials_per_ad for ad in group.ads)


def test_ads_only_consume_their_group_materials():
    groups = plan_structure(
        materials(12),
        config=base_config(
            group_count=2,
            group_material_allocation="SEQUENTIAL_AVERAGE",
            ad_generation_mode="BY_MATERIAL",
            ads_per_group=None,
            ad_material_allocation=None,
            max_materials_per_ad=2,
        ),
        pool=seed_copies(),
        seed=7,
    )

    for group in groups:
        group_materials = set(group.unit_ids)
        assert all(set(ad.unit_ids) <= group_materials for ad in group.ads)
        assert set().union(*(set(ad.unit_ids) for ad in group.ads)) == group_materials


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

    assert len(group.ads) == 2
    for base_ad_no in (1, 2):
        copies = [ad for ad in group.ads if ad.base_ad_no == base_ad_no]
        assert len(copies) == 1
        assert copies[0].unit_ids == tuple(UUID(int=i) for i in range(1, 11))
        assert len(copies[0].copies) == 3
        assert len({copy.text for copy in copies[0].copies}) == 3


def test_unit_ids_are_deduplicated_but_same_name_ids_remain_distinct():
    items = [
        SimpleNamespace(unit_id=UUID(int=value), file_name="Moon.mp4")
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

    assert group.unit_ids == (UUID(int=3), UUID(int=1), UUID(int=2))


def test_material_input_order_is_preserved_and_duplicate_ids_keep_first_record():
    items = [
        SimpleNamespace(unit_id=UUID(int=2), file_name="Beta.mp4"),
        SimpleNamespace(unit_id=UUID(int=1), file_name="Zeta.mp4"),
        SimpleNamespace(unit_id=UUID(int=1), file_name="Alpha.mp4"),
    ]
    config = base_config(
        group_count=1,
        group_material_allocation="SHARED",
        ad_generation_mode="FIXED",
        ads_per_group=1,
        max_materials_per_ad=None,
        ad_material_allocation="SHARED",
    )

    first = plan_structure(items, config=config, pool=seed_copies(), seed=1)
    second = plan_structure(
        [items[1], items[2], items[0]], config=config, pool=seed_copies(), seed=1
    )

    assert first[0].unit_ids == (UUID(int=2), UUID(int=1))
    assert second[0].unit_ids == (UUID(int=1), UUID(int=2))


def test_copy_pool_exhaustion_counts_unique_non_empty_texts():
    config = base_config(
        group_count=1,
        group_material_allocation="SHARED",
        ad_generation_mode="FIXED",
        ads_per_group=1,
        max_materials_per_ad=None,
        ad_material_allocation="SHARED",
        creative_count=3,
    )
    pool = (
        CopyChoice(uuid4(), "First"),
        CopyChoice(uuid4(), "First"),
        CopyChoice(uuid4(), "  "),
        CopyChoice(uuid4(), "Second"),
    )

    with pytest.raises(DomainError, match="copy_pool_exhausted"):
        plan_structure(materials(1), config=config, pool=pool, seed=1)


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


@pytest.mark.parametrize(
    ("groups", "versions", "expected"), [(1, 1, 20), (1, 3, 60), (2, 1, 40)]
)
def test_carousel_atomic_units_and_account_expansion(groups, versions, expected):
    # 60 张图先组成 12 个完整单元，规划器从不查看或切分成员。
    units = [
        SimpleNamespace(
            unit_id=uuid4(), ordered_material_ids=tuple(uuid4() for _ in range(5))
        )
        for _ in range(12)
    ]
    plans = plan_structure(
        units,
        config=base_config(
            group_count=groups, max_materials_per_ad=6, creative_count=versions
        ),
        pool=seed_copies(),
        seed=9,
    )
    assert sum(len(ad.copies) for group in plans for ad in group.ads) * 10 == expected
    assert all(len(ad.unit_ids) == 6 for group in plans for ad in group.ads)
    assert plans[0].unit_ids == tuple(unit.unit_id for unit in units)


def test_same_images_different_music_remain_distinct_units():
    images = (uuid4(), uuid4())
    units = [
        SimpleNamespace(unit_id=uuid4(), ordered_material_ids=images, music_id=song)
        for song in ("a", "b")
    ]
    plans = plan_structure(
        units, config=base_config(max_materials_per_ad=2), pool=seed_copies(), seed=1
    )
    assert plans[0].ads[0].unit_ids == tuple(unit.unit_id for unit in units)


def test_per_ad_contract_is_unit_count_not_member_count():
    with pytest.raises(DomainError, match="50"):
        plan_structure(
            materials(51),
            config=base_config(max_materials_per_ad=51),
            pool=seed_copies(),
            seed=1,
        )
