from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.modules.builds.preview_materials import (
    frozen_ad_material_ids,
    material_limit_exceeded,
)
from app.modules.builds.preview_models import PreviewAdMaterial, PreviewCopy
from app.modules.builds.preview_validation import (
    final_ad_count_exceeded,
    scene_reasons,
)
from app.modules.builds.scene_schemas import SceneContext
from app.modules.strategies.copy_pool import CopyChoice
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.structure import plan_structure


class Material:
    def __init__(self, material_id):
        self.unit_id = material_id
        self.file_name = f"{material_id}.mp4"


def strategy(**changes):
    values = {
        "budget": "100",
        "currency": "USD",
        "bid_strategy": "HIGHEST_VALUE",
        "group_generation_mode": "FIXED",
        "group_count": 2,
        "group_material_allocation": "SHARED",
        "ad_generation_mode": "FIXED",
        "ads_per_group": 2,
        "ad_material_allocation": "SHARED",
        "max_materials_per_group": None,
        "max_materials_per_ad": None,
        "creative_count": 2,
        "copy_pool_version": uuid4(),
    }
    values.update(changes)
    return StrategyConfig(**values)


def copies():
    return tuple(
        CopyChoice(copy_id=uuid4(), text=f"copy-{index}") for index in range(4)
    )


def test_planner_reuses_shared_materials_per_group_and_copies_per_base_ad():
    materials = [Material(uuid4()) for _ in range(20)]
    groups = plan_structure(materials, config=strategy(), pool=copies(), seed=7)

    assert len(groups) == 2
    assert all(len(group.unit_ids) == 20 for group in groups)
    assert all(len(group.ads) == 2 for group in groups)
    assert all(
        len(ad.unit_ids) == 20 and len(ad.copies) == 2
        for group in groups
        for ad in group.ads
    )
    assert {ad.base_ad_no for ad in groups[0].ads} == {1, 2}


def test_planner_rejects_oversized_total_creative_expansion_before_allocating():
    config = strategy(group_count=100, ads_per_group=100, creative_count=100)
    materials = [Material(uuid4())]
    pool = tuple(
        CopyChoice(copy_id=uuid4(), text=f"copy-{index}") for index in range(100)
    )

    with pytest.raises(DomainError, match="策略展开广告数量"):
        plan_structure(materials, config=config, pool=pool, seed=7)


def test_planner_deduplicates_material_rows_before_allocating():
    material_id = uuid4()
    groups = plan_structure(
        [Material(material_id), Material(material_id), Material(uuid4())],
        config=strategy(group_count=1, ads_per_group=1),
        pool=copies(),
        seed=3,
    )

    assert groups[0].unit_ids == (material_id, groups[0].unit_ids[1])
    assert groups[0].ads[0].unit_ids == groups[0].unit_ids


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
    reasons = scene_reasons(
        strategy(cta_option_ids=("cta",), creative_count=31), scene, "USD"
    )
    assert "roas_limits_unverified" not in reasons
    assert "roas_out_of_range" not in reasons
    assert "creative_count_exceeded" not in reasons


def test_final_ad_limit_uses_base_ads_times_creative_count():
    config = strategy(
        group_count=1,
        ad_generation_mode="BY_MATERIAL",
        ads_per_group=None,
        ad_material_allocation=None,
        max_materials_per_ad=1,
        creative_count=3,
    )
    groups = plan_structure(
        [Material(uuid4()) for _ in range(20)],
        config=config,
        pool=copies(),
        seed=11,
    )
    assert len(groups[0].ads) == 20
    assert final_ad_count_exceeded(
        base_ad_count=len(groups[0].ads),
        creative_count=config.creative_count,
        maximum=30,
    )


def test_preview_ad_material_migration_installs_frozen_child_trigger():
    migration = (
        Path(__file__).resolve().parents[3]
        / "app/alembic/versions/20261005_preview_ad_material_strategy.py"
    ).read_text()
    assert (
        "CREATE TRIGGER preview_ad_material_frozen BEFORE INSERT OR UPDATE OR DELETE"
        in migration
    )
    assert "check_preview_child_write()" in migration
    assert "DROP TRIGGER preview_ad_material_frozen ON preview_ad_material" in migration
    assert (
        'op.alter_column("preview_copy", "base_ad_no", server_default=None)'
        in migration
    )
    assert (
        'op.alter_column("planned_ad", "base_ad_no", server_default=None)' in migration
    )
    assert "base_ad_no contains non-legacy identities" in migration
    assert "legacy ad keys would conflict" in migration


def test_ad_material_models_keep_ad_base_in_primary_key():
    assert "base_ad_no" in {column.name for column in PreviewCopy.__table__.primary_key}
    assert "base_ad_no" in {
        column.name for column in PreviewAdMaterial.__table__.primary_key
    }


def test_legacy_frozen_group_materials_are_used_when_ad_rows_are_absent():
    class Result:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

        def first(self):
            return self.values[0] if self.values else None

    class LegacySession:
        def get(self, *args):
            return SimpleNamespace(version=1, legacy_material_layout="GROUP")

        def __init__(self):
            self.calls = 0

        def exec(self, query):
            self.calls += 1
            return Result([uuid4(), uuid4()])

    assert (
        len(
            frozen_ad_material_ids(
                LegacySession(),
                tenant_id=uuid4(),
                preview_id=uuid4(),
                drama_id=uuid4(),
                group_no=1,
                base_ad_no=1,
            )
        )
        == 2
    )


def test_partial_ad_rows_in_another_drama_disable_legacy_fallback():
    class Result:
        def all(self):
            return []

        def first(self):
            return uuid4()

    class Session:
        def get(self, *args):
            return SimpleNamespace(version=1, legacy_material_layout="AD")

        def exec(self, query):
            return Result()

    assert (
        frozen_ad_material_ids(
            Session(),
            tenant_id=uuid4(),
            preview_id=uuid4(),
            drama_id=uuid4(),
            group_no=1,
            base_ad_no=1,
        )
        == []
    )


def test_frozen_ad_material_ids_reads_skipped_materials_in_one_batch():
    first_id, skipped_id, last_id = uuid4(), uuid4(), uuid4()

    class Result:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

        def first(self):
            return self.values[0] if self.values else None

    class Session:
        def get(self, *args):
            return SimpleNamespace(version=1, legacy_material_layout="AD")

        def __init__(self):
            self.calls = 0

        def exec(self, query):
            self.calls += 1
            # ad rows, preview-level presence, then one batched skipped-ID query
            return Result(
                [first_id, skipped_id, last_id] if self.calls == 1 else [skipped_id]
            )

    session = Session()
    assert frozen_ad_material_ids(
        session,
        tenant_id=uuid4(),
        preview_id=uuid4(),
        drama_id=uuid4(),
        group_no=1,
        base_ad_no=1,
        unit_id=uuid4(),
    ) == [first_id, last_id]
    assert session.calls == 2


def test_material_limit_uses_frozen_scene_limit():
    assert material_limit_exceeded([10], 9)
    assert not material_limit_exceeded([10], 10)


def test_batch_limit_counts_all_dramas_accounts_and_copy_versions():
    from app.modules.strategies.structure import estimate_batch_structure

    config = strategy(group_count=2, ads_per_group=2, creative_count=3)
    result = estimate_batch_structure([10, 20], account_count=10, config=config)
    assert (result.campaign_count, result.adgroup_count, result.ad_count) == (
        20,
        40,
        240,
    )
    assert result.daily_budget_sum == config.budget * 20
    grouped = estimate_batch_structure(
        [10, 20],
        account_count=10,
        config=config.model_copy(update={"budget_strategy": "ADGROUP"}),
    )
    assert grouped.daily_budget_sum == config.budget * 40
    with pytest.raises(DomainError, match="拆批"):
        estimate_batch_structure([10] * 100, account_count=100, config=config)


def test_video_content_rejects_music_and_pending_status():
    from pydantic import ValidationError

    from app.modules.builds.image_composition_schemas import CreativeUnitContent

    values = {
        "kind": "VIDEO",
        "ordered_material_ids": (uuid4(),),
        "content_digest": "a",
        "position": 1,
        "music_status": "NOT_REQUIRED",
    }
    assert CreativeUnitContent(**values).music_id is None
    for changes in ({"music_id": "song"}, {"music_status": "PENDING"}):
        with pytest.raises(ValidationError):
            CreativeUnitContent(**{**values, **changes})


def test_video_scene_cannot_qualify_carousel():
    scene = SceneContext(
        supported=True, reason_codes=(), capability_revision="old-video"
    )
    assert "scene_format_unverified" in scene_reasons(
        strategy(), scene, "USD", media_kind="CAROUSEL"
    )


def test_estimate_endpoint_uses_server_planner_and_bounds_counts(client, context):
    from tests.modules.strategies.test_api import headers

    path = f"/api/tenants/{context.tenant_id}/strategies/structure-estimate"
    config = strategy(
        group_count=1,
        ad_generation_mode="BY_MATERIAL",
        ads_per_group=None,
        ad_material_allocation=None,
        max_materials_per_ad=6,
        creative_count=3,
    )
    body = {
        "config": config.model_dump(mode="json"),
        "unit_counts": [12],
        "account_count": 10,
    }
    result = client.post(path, json=body, headers=headers(context))
    assert result.status_code == 200, result.text
    assert result.json() == {
        "campaign_count": 10,
        "adgroup_count": 10,
        "ad_count": 60,
        "daily_budget_sum": "1000",
        "dramas": [[[6, 6]]],
    }
    result = client.post(
        path, json={**body, "unit_counts": [100_001]}, headers=headers(context)
    )
    assert result.status_code == 422


@pytest.mark.parametrize("action", ["PREPARE", "RETRY_FAILED"])
def test_preview_retry_preserves_applied_pointer_without_hiding_bulk_music(
    session, context, intent, action
):
    from app.modules.builds.carousel_music_models import MusicPreparationRun
    from app.modules.builds.previews import _bump_preview_retry_revision
    from tests.modules.builds.test_image_composition_migration import scaffold

    draft, drama = scaffold(session, context, intent)
    draft.active_image_generation = 1
    draft.applied_image_rule_revision = draft.revision
    run = MusicPreparationRun(
        tenant_id=context.tenant_id,
        draft_id=draft.id,
        bc_id=draft.bc_id,
        actor_id=context.actor_id,
        active_generation=1,
        route={},
        action=action,
        base_revision=draft.revision,
        drama_ids=[str(drama.drama_id)],
    )
    session.add(run)
    session.flush()
    before = draft.revision
    with pytest.raises(
        DomainError, check=lambda e: e.code == "music_preparation_pending"
    ):
        _bump_preview_retry_revision(session, draft, before)
    assert draft.revision == before
    assert run.status == "PENDING"
    run.status = "READY"
    session.flush()
    _bump_preview_retry_revision(session, draft, before)
    assert draft.applied_image_rule_revision == draft.revision == before + 1
    draft.applied_image_rule_revision = before
    _bump_preview_retry_revision(session, draft, draft.revision)
    assert (
        draft.applied_image_rule_revision == before
    )  # 原输入已失效时不能伪造重新应用。


def test_budget_estimate_preserves_decimal_precision_and_zero_accounts():
    from app.modules.strategies.structure import estimate_batch_structure

    result = estimate_batch_structure(
        [1],
        account_count=3,
        config=strategy(group_count=1, budget="9007199254740993123456.123456789012"),
    )
    assert str(result.daily_budget_sum) == "27021597764222979370368.370370367036"
    assert (
        estimate_batch_structure(
            [100_000] * 1000, account_count=0, config=strategy()
        ).ad_count
        == 0
    )
