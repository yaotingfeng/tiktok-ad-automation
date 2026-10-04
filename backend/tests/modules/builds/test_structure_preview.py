from uuid import uuid4

from app.modules.builds.preview_materials import (
    frozen_ad_material_ids,
    material_limit_exceeded,
)
from app.modules.builds.preview_models import PreviewAdMaterial, PreviewCopy
from app.modules.builds.preview_validation import scene_reasons
from app.modules.builds.scene_schemas import SceneContext
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


def test_highest_value_without_roas_does_not_require_roas_limits():
    scene = SceneContext(
        supported=True,
        reason_codes=(),
        capability_revision="test",
        creative_limit=50,
        copy_length_limit=100,
        field_constraints={
            "max_ads_per_adgroup": 30,
            "campaign_daily_budget": {
                "currency": "USD",
                "minimum_inclusive": "1",
                "maximum_exclusive": "1000",
                "precision": "1",
            },
        },
        cta_fields={"asset_ids": ["cta"]},
    )
    reasons = scene_reasons(strategy(cta_option_ids=("cta",)), scene, "USD")
    assert "roas_limits_unverified" not in reasons
    assert "roas_out_of_range" not in reasons


def test_ad_material_models_keep_ad_base_in_primary_key():
    assert "base_ad_no" in {column.name for column in PreviewCopy.__table__.primary_key}
    assert "base_ad_no" in {column.name for column in PreviewAdMaterial.__table__.primary_key}


def test_legacy_frozen_group_materials_are_used_when_ad_rows_are_absent():
    class Result:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

        def first(self):
            return self.values[0] if self.values else None

    class LegacySession:
        def __init__(self):
            self.calls = 0

        def exec(self, query):
            self.calls += 1
            return Result([] if self.calls < 3 else [uuid4(), uuid4()])

    assert len(
        frozen_ad_material_ids(
            LegacySession(),
            tenant_id=uuid4(),
            preview_id=uuid4(),
            drama_id=uuid4(),
            group_no=1,
            base_ad_no=1,
        )
    ) == 2


def test_material_limit_uses_frozen_scene_limit():
    assert material_limit_exceeded([10], 9)
    assert not material_limit_exceeded([10], 10)
