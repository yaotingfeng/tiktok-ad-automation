"""Pure composition contracts: ordering, identity, exclusions and independent music fences."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.modules.builds.image_composition import compose_images, resolve_image_rule
from app.modules.builds.image_composition_schemas import (
    CreativeUnit,
    ImageCandidate,
    ImageRule,
)

STAMP = datetime(2026, 10, 1, tzinfo=UTC)


def candidates(count):
    return tuple(
        ImageCandidate(
            material_id=UUID(int=i),
            file_name_snapshot=f"image-{i}.jpg",
            upload_batch_id=UUID(int=10000),
            upload_batch_created_at=STAMP,
            upload_sequence=i - 1,
            created_at=STAMP,
            content_digest=f"image-{i}",
        )
        for i in range(1, count + 1)
    )


def persisted(unit, identity=100, **updates):
    return CreativeUnit.model_validate(
        unit.model_dump() | {"id": UUID(int=identity)} | updates
    )


def groups(diff):
    return [tuple(x.int for x in unit.ordered_material_ids) for unit in diff.units]


@pytest.mark.parametrize(
    "count,remainder,want,excluded",
    [(60, "KEEP", 12, 0), (61, "KEEP", 13, 0), (61, "EXCLUDE", 12, 1)],
)
def test_sequential_counts_and_tail(count, remainder, want, excluded):
    diff = compose_images(
        candidates(count), ImageRule(images_per_unit=5, remainder=remainder), ()
    )
    assert len(diff.units) == want
    assert len(diff.excluded_images) == excluded
    assert all(unit.id is None for unit in diff.units)
    if count == 61 and remainder == "KEEP":
        assert groups(diff)[-1] == (61,)
        assert diff.units[-1].music_status == "NEEDS_SELECTION"
    if excluded:
        assert diff.excluded_images[0].material_id == UUID(int=61)
        assert diff.excluded_images[0].reason == "REMAINDER"


@pytest.mark.parametrize(
    "values",
    [
        {"images_per_unit": 0},
        {"images_per_unit": 36},
        {"images_per_unit": 1},
        {"mode": "SINGLE", "images_per_unit": 2},
    ],
)
def test_invalid_rule_boundaries(values):
    with pytest.raises(ValidationError):
        ImageRule(**values)


def test_single_rule_and_explicit_field_patch():
    batch = ImageRule(images_per_unit=5, music_mode="FIXED", fixed_music_id="track")
    rule = resolve_image_rule(batch, {"mode": "SINGLE", "images_per_unit": 1})
    diff = compose_images(candidates(2), rule, ())
    assert groups(diff) == [(1,), (2,)]
    assert [u.music_id for u in diff.units] == ["track", "track"]
    assert rule.order == batch.order
    assert resolve_image_rule(batch, {"fixed_music_id": None}).fixed_music_id is None
    with pytest.raises(ValidationError):
        resolve_image_rule(batch, {"unknown": 3})
    with pytest.raises(ValidationError):
        resolve_image_rule(batch, {"images_per_unit": None})


def test_natural_order_ties_unicode_and_page_merge_are_stable():
    rows = candidates(6)
    names = ["2.jpg", "10.jpg", "02.jpg", "２.jpg", "a2.jpg", "A2.jpg"]
    rows = tuple(
        row.model_copy(update={"file_name_snapshot": name})
        for row, name in zip(rows, names, strict=True)
    )
    rule = ImageRule(images_per_unit=3)
    first = compose_images(rows, rule, ())
    assert groups(first) == [(3, 1, 4), (2, 6, 5)]
    assert compose_images(rows[3:] + rows[:3] + rows[:1], rule, ()) == first


def test_upload_order_uses_batch_time_then_index_and_reports_old_fallback():
    rows = candidates(4)
    rows = (
        rows[0].model_copy(
            update={"upload_batch_created_at": STAMP + timedelta(days=1)}
        ),
        rows[1].model_copy(update={"upload_sequence": 8}),
        rows[2].model_copy(update={"upload_sequence": 2}),
        rows[3].model_copy(
            update={"upload_sequence": None, "created_at": STAMP - timedelta(days=1)}
        ),
    )
    diff = compose_images(
        rows, ImageRule(order="UPLOAD_SEQUENCE", images_per_unit=2), ()
    )
    assert groups(diff) == [(4, 3), (2, 1)]
    assert [(w.material_id, w.reason) for w in diff.warnings] == [
        (UUID(int=4), "UPLOAD_SEQUENCE_MISSING")
    ]


def test_composition_protection_reserves_images_and_preserves_duplicate_identities():
    rule = ImageRule(images_per_unit=2)
    initial = compose_images(candidates(4), rule, ())
    first = persisted(initial.units[0], 101, composition_protected=True, position=8)
    copy = persisted(initial.units[0], 102, composition_protected=True, position=7)
    diff = compose_images(candidates(4), rule, (first, copy))
    assert groups(diff) == [(1, 2), (1, 2), (3, 4)]
    assert [u.id for u in diff.units] == [copy.id, first.id, None]
    assert diff.retained_ids == (copy.id, first.id)
    assert [u.position for u in diff.units] == [1, 2, 3]


def test_identity_only_reused_for_ordered_combos_and_music_rule_fences():
    rule = ImageRule(images_per_unit=2, music_mode="FIXED", fixed_music_id="A")
    old = persisted(
        compose_images(candidates(2), rule, ()).units[0],
        music_revision=4,
        music_request_generation=2,
    )
    retained = compose_images(candidates(2), rule, (old,)).units[0]
    assert retained.id == old.id and retained.music_id == "A"
    assert retained.music_revision == 4 and retained.music_request_generation == 2
    changed = compose_images(
        candidates(2), resolve_image_rule(rule, {"fixed_music_id": "B"}), (old,)
    ).units[0]
    assert changed.id == old.id and changed.music_id == "B"
    assert changed.music_request_generation == 3
    assert changed.revision == old.revision  # 音乐规则变化不伪造内容版本。
    reversed_old = old.model_copy(
        update={"ordered_material_ids": tuple(reversed(old.ordered_material_ids))}
    )
    diff = compose_images(candidates(2), rule, (reversed_old,))
    assert diff.units[0].id is None and diff.removed_ids == (old.id,)


def test_protected_music_blocks_disappearing_combo_and_replace_all_lists_loss():
    rule = ImageRule(images_per_unit=2)
    old = persisted(
        compose_images(candidates(4), rule, ()).units[0],
        music_id="manual",
        music_protected=True,
        music_status="READY",
    )
    new_rule = ImageRule(images_per_unit=3)
    diff = compose_images(candidates(4), new_rule, (old,))
    assert [(c.code, c.unit_id) for c in diff.conflicts] == [
        ("PROTECTED_MUSIC_COMPOSITION_CHANGED", old.id)
    ]
    replaced = compose_images(candidates(4), new_rule, (old,), mode="REPLACE_ALL")
    assert replaced.conflicts == () and replaced.removed_ids == (old.id,)
    assert replaced.protection_losses == (old.id,)


def test_disabled_candidates_never_revive_even_replace_all_and_empty_blocks():
    rows = tuple(c.model_copy(update={"enabled": False}) for c in candidates(2))
    diff = compose_images(rows, ImageRule(), (), mode="REPLACE_ALL")
    assert not diff.units
    assert [x.reason for x in diff.excluded_images] == ["DISABLED", "DISABLED"]
    assert [c.code for c in diff.conflicts] == ["NO_IMAGES"]


def test_missing_protected_image_is_conflict_not_silent_recut():
    rule = ImageRule(images_per_unit=2)
    old = persisted(
        compose_images(candidates(2), rule, ()).units[0], composition_protected=True
    )
    diff = compose_images(candidates(1), rule, (old,))
    assert groups(diff) == [(1, 2)]
    assert diff.conflicts[0].code == "PROTECTED_IMAGE_UNAVAILABLE"


def test_music_only_protection_survives_rule_change_for_same_combo():
    rule = ImageRule(images_per_unit=2)
    old = persisted(
        compose_images(candidates(2), rule, ()).units[0],
        music_protected=True,
        music_id="manual",
        music_status="READY",
    )
    diff = compose_images(
        candidates(2),
        resolve_image_rule(rule, {"music_mode": "FIXED", "fixed_music_id": "other"}),
        (old,),
    )
    assert diff.units[0].music_id == "manual" and diff.units[0].music_protected
    assert not diff.conflicts


def test_unit_rejects_duplicate_members_and_video_cannot_enter_image_compose():
    first = compose_images(candidates(2), ImageRule(images_per_unit=2), ()).units[0]
    with pytest.raises(ValidationError):
        persisted(first, ordered_material_ids=(UUID(int=1), UUID(int=1)))
    video = persisted(
        first,
        kind="VIDEO",
        ordered_material_ids=(UUID(int=1),),
        music_status="NOT_REQUIRED",
    )
    with pytest.raises(ValueError, match="CAROUSEL"):
        compose_images(candidates(2), ImageRule(), (video,))


def test_replace_all_resets_manual_music_even_when_original_rule_is_unchanged():
    rule = ImageRule(images_per_unit=2, music_mode="FIXED", fixed_music_id="batch")
    initial = compose_images(candidates(2), rule, ()).units[0]
    old = persisted(
        initial, music_id="manual", music_protected=True, music_status="READY"
    )
    diff = compose_images(candidates(2), rule, (old,), mode="REPLACE_ALL")
    assert diff.units[0].id == old.id
    assert diff.units[0].music_id == "batch"
    assert diff.units[0].music_request_generation == old.music_request_generation + 1
    assert not diff.units[0].music_protected


def test_identical_input_is_deterministic_and_irrelevant_music_fields_do_not_reset():
    rule = ImageRule(images_per_unit=2)
    initial = compose_images(candidates(2), rule, ())
    assert initial == compose_images(tuple(reversed(candidates(2))), rule, ())
    old = persisted(
        initial.units[0], music_id="recommended", music_status="READY", music_revision=7
    )
    changed = resolve_image_rule(rule, {"single_music_id": "for-singles"})
    result = compose_images(candidates(2), changed, (old,)).units[0]
    assert result.music_id == "recommended" and result.music_revision == 7


def test_candidate_snapshot_disagreement_does_not_depend_on_page_order():
    original = candidates(1)[0]
    with pytest.raises(ValueError, match="快照不一致"):
        compose_images(
            (original, original.model_copy(update={"enabled": False})), ImageRule(), ()
        )
