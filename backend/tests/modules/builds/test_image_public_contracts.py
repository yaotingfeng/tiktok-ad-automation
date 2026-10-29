from uuid import uuid4

import pytest
from pydantic import ValidationError

from tests.modules.builds.test_image_composition_service import apply_rule
from tests.modules.builds.test_image_composition_service import (
    image_draft as image_draft,
)


def test_structure_save_is_independent_and_idempotent(session, context, image_draft):
    from app.modules.builds.catalog import draft_summary
    from app.modules.builds.image_composition_service import (
        composition_state,
        list_creative_units,
    )
    from app.modules.builds.structure import ImageStructureChange, save_image_structure
    from app.modules.strategies.models import StrategyVersion

    draft, _, scope = image_draft
    apply_rule(session, image_draft)
    before = list_creative_units(session, scope).model_dump()
    state = composition_state(session, scope)
    strategy = session.get(StrategyVersion, draft.strategy_version_id)
    original = strategy.model_dump()
    body = ImageStructureChange(
        request_id=uuid4(),
        expected_revision=draft.revision,
        structure_override={
            "group_generation_mode": "FIXED",
            "group_count": 2,
            "group_material_allocation": "SHARED",
            "max_materials_per_group": None,
        },
    )
    revision = save_image_structure(session, context=context, draft_id=draft.id, body=body)
    assert revision == body.expected_revision + 1
    assert (
        save_image_structure(session, context=context, draft_id=draft.id, body=body)
        == revision
    )
    assert list_creative_units(session, scope).model_dump() == before
    after = composition_state(session, scope)
    assert after["snapshot_id"] == state["snapshot_id"]
    assert after["applied_rule_revision"] == revision
    assert strategy.model_dump() == original
    assert (
        draft_summary(
            session, context=context, draft_id=draft.id
        ).structure_override.group_count
        == 2
    )
    clear = ImageStructureChange(
        request_id=uuid4(), expected_revision=revision, structure_override={}
    )
    save_image_structure(session, context=context, draft_id=draft.id, body=clear)
    assert draft.structure_override == {}


def test_structure_only_fields_and_effective_strategy():
    from app.modules.builds.structure import (
        StructureOverride,
        effective_structure_config,
    )
    from app.modules.strategies.schemas import StrategyConfig

    for invalid in ({"budget": "50"}, {"identity_id": "x"}, {"group_count": True}):
        with pytest.raises(ValidationError):
            StructureOverride.model_validate(invalid)
    base = StrategyConfig(budget="50", currency="USD", copy_pool_version=uuid4())
    assert effective_structure_config(base, {}).model_dump() == base.model_dump()
    assert effective_structure_config(base, {"group_count": 2}).group_count == 2
    with pytest.raises(ValidationError):
        effective_structure_config(base, {"group_generation_mode": "BY_MATERIAL"})


def test_public_music_snapshot_drops_internal_intent():
    from app.modules.builds.image_public import PublicMusicSnapshot

    result = PublicMusicSnapshot.model_validate(
        {
            "display_name": "Song",
            "pending_request": {"secret": "internal"},
            "preview_url": "private",
        }
    )
    assert result.model_dump(exclude_none=True) == {"display_name": "Song"}


def test_openapi_composition_responses_are_typed():
    from app.main import app

    schema = app.openapi()
    paths = {
        path: operations
        for path, operations in schema["paths"].items()
        if "image-composition" in path or "/music" in path or "carousel-presets" in path
    }
    assert paths
    for operations in paths.values():
        for operation in operations.values():
            response = operation["responses"]["200"]["content"]["application/json"][
                "schema"
            ]
            assert response.get("$ref"), operation["operationId"]


@pytest.mark.parametrize("complete", [False, True])
def test_structure_preserves_current_refresh(session, context, image_draft, complete):
    from app.modules.builds.image_candidate_service import candidate_snapshot_progress
    from app.modules.builds.image_composition_service import adopt_candidates
    from app.modules.builds.structure import ImageStructureChange, save_image_structure
    from tests.modules.builds.test_image_composition_service import (
        finish_candidate_snapshot,
    )

    draft, _, scope = image_draft
    from app.modules.builds.image_candidate_service import (
        snapshot_progress,
        start_candidate_snapshot,
    )

    pending = snapshot_progress(start_candidate_snapshot(session, scope, draft))
    if complete:
        finish_candidate_snapshot(session, scope, pending)
    body = ImageStructureChange(
        request_id=uuid4(),
        expected_revision=draft.revision,
        structure_override={"creative_count": 3},
    )
    revision = save_image_structure(session, context=context, draft_id=draft.id, body=body)
    progress = candidate_snapshot_progress(session, scope, pending["snapshot_id"])
    assert progress["revision"] == revision
    assert progress["status"] == ("READY" if complete else "PENDING")
    assert (
        save_image_structure(session, context=context, draft_id=draft.id, body=body)
        == revision
    )
    finish_candidate_snapshot(session, scope, progress)
    adopt_candidates(session, scope, uuid4(), revision, pending["snapshot_id"])
    assert draft.revision == revision + 1
    assert (
        candidate_snapshot_progress(session, scope, pending["snapshot_id"])["revision"]
        == revision
    )


def test_structure_busy_stale_replay_and_unapplied(session, context, image_draft):
    from app.core.errors import DomainError
    from app.modules.builds.image_composition_schemas import ImageRule
    from app.modules.builds.image_composition_service import (
        apply_composition,
        preview_composition,
    )
    from app.modules.builds.structure import ImageStructureChange, save_image_structure

    draft, _, scope = image_draft
    body = ImageStructureChange(
        request_id=uuid4(),
        expected_revision=draft.revision,
        structure_override={"creative_count": 3},
    )
    original_applied = draft.applied_image_rule_revision
    save_image_structure(session, context=context, draft_id=draft.id, body=body)
    assert draft.applied_image_rule_revision == original_applied
    with pytest.raises(DomainError, match="不同参数"):
        save_image_structure(
            session,
            context=context,
            draft_id=draft.id,
            body=body.model_copy(update={"expected_revision": draft.revision}),
        )
    with pytest.raises(DomainError, match="更新"):
        save_image_structure(
            session,
            context=context,
            draft_id=draft.id,
            body=body.model_copy(update={"request_id": uuid4()}),
        )
    plan = preview_composition(session, scope, uuid4(), draft.revision, ImageRule(), {})
    apply_composition(session, scope, plan["plan_id"], uuid4(), draft.revision)
    with pytest.raises(DomainError, match="正在应用"):
        save_image_structure(
            session,
            context=context,
            draft_id=draft.id,
            body=ImageStructureChange(
                request_id=uuid4(),
                expected_revision=draft.revision,
                structure_override={},
            ),
        )


def test_public_endpoints_serialize_real_units_plan_candidates(
    session, context, client, image_draft
):
    from app.api.deps import get_current_user
    from app.main import app
    from app.models import User
    from app.modules.builds.image_composition_schemas import ImageRule
    from app.modules.builds.image_composition_service import preview_composition

    draft, drama, scope = image_draft
    apply_rule(session, image_draft)
    plan = preview_composition(session, scope, uuid4(), draft.revision, ImageRule(), {})
    app.dependency_overrides[get_current_user] = lambda: session.get(
        User, context.actor_id
    )
    url = f"/api/tenants/{context.tenant_id}/build-drafts/{draft.id}/image-composition"
    for suffix in (
        "",
        "/units",
        "/candidates",
        f"/plans/{plan['plan_id']}",
        f"/plans/{plan['plan_id']}/units",
        f"/plans/{plan['plan_id']}/differences",
    ):
        response = client.get(url + suffix)
        assert response.status_code == 200, response.text
        assert "pending_request" not in response.text
    filtered = client.get(url + f"/units?drama_id={drama.drama_id}&limit=1").json()
    assert len(filtered["items"]) == 1 and filtered["next_cursor"]
    assert client.get(url + f"/units?drama_id={uuid4()}").json()["total"] == 0
    for endpoint in ("units", "candidates"):
        page = client.get(
            url + f"/{endpoint}", params={"drama_id": str(drama.drama_id), "limit": 1}
        ).json()
        next_page = client.get(
            url + f"/{endpoint}",
            params={
                "drama_id": str(drama.drama_id),
                "cursor": page["next_cursor"],
                "limit": 1,
            },
        )
        assert next_page.status_code == 200 and len(next_page.json()["items"]) == 1
        assert (
            client.get(
                url + f"/{endpoint}",
                params={"drama_id": str(uuid4()), "cursor": page["next_cursor"]},
            ).status_code
            == 422
        )

    summary = client.get(url.removesuffix("/image-composition")).json()
    assert summary["structure_override"] == {}
    assert client.get(url + "/dramas").json()["items"][0]["candidate_count"] == 7


def test_drama_aggregate_does_not_multiply_reused_images(session, image_draft):
    from app.modules.builds.image_composition_commands import Duplicate
    from app.modules.builds.image_composition_queries import drama_summaries
    from app.modules.builds.image_composition_service import edit_creative_units

    draft, drama, scope = image_draft
    units = apply_rule(session, image_draft)
    edit_creative_units(
        session,
        scope,
        uuid4(),
        draft.revision,
        Duplicate(kind="DUPLICATE", unit_id=units[0]["id"]),
    )
    result = drama_summaries(session, scope).items[0]
    assert result["candidate_count"] == 7
    assert result["unit_count"] == 4
    assert result["reference_count"] == 10
    assert result["used_image_count"] == 7
    assert result["unused_count"] == 0
    assert result["protected_count"] == 1
    assert drama_summaries(session, scope, issue="unused").total == 0
    assert drama_summaries(session, scope, issue="protected").total == 1


def test_drama_summary_cursor_rejects_changed_filter(session, image_draft):
    from app.core.errors import DomainError
    from app.modules.accounts.resolver import encode_cursor
    from app.modules.builds.image_composition_queries import drama_summaries

    draft, _, scope = image_draft
    cursor = encode_cursor(
        scope={
            "tenant": str(scope.tenant_id),
            "draft": str(scope.draft_id),
            "bc": scope.bc_id,
            "kind": f"image-dramas:{draft.revision}:{draft.active_image_generation}:all",
        },
        last_id="1",
    )
    with pytest.raises(DomainError):
        drama_summaries(session, scope, issue="music", cursor=cursor)
    assert drama_summaries(session, scope, cursor=cursor).items == []


@pytest.mark.usefixtures("image_draft")
def test_material_picker_filters_media_and_binds_cursor(session, context, client):
    from app.api.deps import get_current_user
    from app.main import app
    from app.models import User
    from tests.modules.materials.test_tenant_materials import material

    material(session, context, "Moon-video.mp4")
    app.dependency_overrides[get_current_user] = lambda: session.get(
        User, context.actor_id
    )
    url = f"/api/tenants/{context.tenant_id}/materials"
    response = client.get(url, params={"media_type": "IMAGE", "limit": 1})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 7 and data["items"][0]["media_type"] == "IMAGE"
    assert (
        client.get(
            url, params={"media_type": "VIDEO", "cursor": data["next_cursor"]}
        ).status_code
        == 422
    )
    video = client.get(url, params={"media_type": "VIDEO"}).json()
    assert video["total"] == 1 and video["items"][0]["media_type"] == "VIDEO"


def test_structure_rejects_video_and_does_not_rebase_old_snapshot(
    session, context, image_draft
):
    from app.core.errors import DomainError
    from app.modules.builds.image_candidate_service import start_candidate_snapshot
    from app.modules.builds.structure import ImageStructureChange, save_image_structure

    draft, _, scope = image_draft
    snapshot = start_candidate_snapshot(session, scope, draft)
    snapshot.base_revision = draft.revision - 1
    snapshot.status = "FAILED"
    old = snapshot.base_revision
    body = ImageStructureChange(
        request_id=uuid4(),
        expected_revision=draft.revision,
        structure_override={"creative_count": 3},
    )
    save_image_structure(session, context=context, draft_id=draft.id, body=body)
    assert snapshot.base_revision == old and snapshot.status == "FAILED"
    draft.media_kind = "VIDEO"
    with pytest.raises(DomainError):
        save_image_structure(
            session,
            context=context,
            draft_id=draft.id,
            body=ImageStructureChange(
                request_id=uuid4(),
                expected_revision=draft.revision,
                structure_override={},
            ),
        )


def test_drama_summary_pages_only_current_inputs(session, context):
    from sqlmodel import select

    from app.modules.builds.image_composition_queries import drama_summaries
    from app.modules.builds.image_composition_service import resolve_image_scope
    from app.modules.builds.models import DraftInput
    from tests.modules.builds.test_drafts import create_intent
    from tests.modules.builds.test_image_composition_migration import scaffold

    intent = create_intent(session, context)
    intent["drama_lines"] = ["Moon", "Sun", "Historical"]
    draft, _ = scaffold(session, context, intent)
    historical = session.exec(
        select(DraftInput).where(
            DraftInput.draft_id == draft.id,
            DraftInput.kind == "drama",
            DraftInput.line_no == 3,
        )
    ).one()
    historical.drama_id = None
    session.flush()
    scope = resolve_image_scope(session, context=context, draft_id=draft.id)
    first = drama_summaries(session, scope, limit=1)
    assert first.total == 2 and first.next_cursor
    assert first.items[0]["title"] == "Moon"
    second = drama_summaries(session, scope, cursor=first.next_cursor, limit=1)
    assert second.items[0]["title"] == "Sun" and second.next_cursor is None
    assert second.items[0]["candidate_count"] == 0
