from uuid import uuid4

from app.modules.strategies.copy_pool import CopyChoice
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.structure import plan_structure


class Material:
    def __init__(self, material_id):
        self.material_id = material_id
        self.file_name = f"{material_id}.mp4"


def strategy(**changes):
    return StrategyConfig(
        budget="100",
        currency="USD",
        bid_strategy="HIGHEST_VALUE",
        group_generation_mode="FIXED",
        group_count=2,
        group_material_allocation="SHARED",
        ad_generation_mode="FIXED",
        ads_per_group=2,
        ad_material_allocation="SHARED",
        max_materials_per_group=None,
        max_materials_per_ad=None,
        creative_count=2,
        copy_pool_version=uuid4(),
        **changes,
    )


def copies():
    return tuple(CopyChoice(copy_id=uuid4(), text=f"copy-{index}") for index in range(4))


def test_planner_reuses_shared_materials_per_group_and_copies_per_base_ad():
    materials = [Material(uuid4()) for _ in range(20)]
    groups = plan_structure(materials, config=strategy(), pool=copies(), seed=7)

    assert len(groups) == 2
    assert all(len(group.material_ids) == 20 for group in groups)
    assert all(len(group.ads) == 2 for group in groups)
    assert all(len(ad.material_ids) == 20 and len(ad.copies) == 2 for group in groups for ad in group.ads)
    assert {ad.base_ad_no for ad in groups[0].ads} == {1, 2}


def test_planner_deduplicates_material_rows_before_allocating():
    material_id = uuid4()
    groups = plan_structure(
        [Material(material_id), Material(material_id), Material(uuid4())],
        config=strategy(group_count=1, ads_per_group=1),
        pool=copies(),
        seed=3,
    )

    assert groups[0].material_ids == (material_id, groups[0].material_ids[1])
    assert groups[0].ads[0].material_ids == groups[0].material_ids
