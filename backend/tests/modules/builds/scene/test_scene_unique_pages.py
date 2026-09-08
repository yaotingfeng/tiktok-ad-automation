"""Full-list uniqueness across every paginated scene resource."""

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.builds.scene_models import SceneReadState
from tests.modules.builds.scene.test_scene_reads import page, refresh
from tests.modules.builds.scene.test_scene_reads import scene_env as scene_env
from tests.modules.builds.scene.test_scene_reads import source_env as source_env
from tests.modules.builds.scene.test_scene_reads import wire as wire


@pytest.mark.parametrize(
    "resource,id_field", [("minis", "minis_id"), ("account_roles", "asset_id")]
)
@pytest.mark.parametrize("cross_page", [False, True])
def test_every_paged_resource_rejects_repeated_unselected_ids(
    scene_env, wire, redis_client, resource, id_field, cross_page
):
    if cross_page:
        wire[1].append(
            page([{id_field: f"unselected-{n}"} for n in range(50)], total=2)
        )
        first = refresh(scene_env, redis_client, resource)
        assert first.next_page == 2
        wire[1].append(page([{id_field: "unselected-0"}], number=2, total=2))
        result = refresh(scene_env, redis_client, resource, first.evidence_id)
    else:
        wire[1].append(page([{id_field: "unselected"}, {id_field: "unselected"}]))
        result = refresh(scene_env, redis_client, resource)
    assert not result.complete and result.reason_codes


def test_cross_page_id_proof_checks_all_prior_pages_and_is_bounded(
    scene_env, wire, redis_client
):
    from app.modules.builds.scene_models import SceneEvidence

    first_id = None
    for number in (1, 2, 3):
        items = (
            [{"minis_id": f"unselected-{number}-{n}"} for n in range(50)]
            if number < 3
            else [{"minis_id": "unselected-1-0"}]
        )
        payload = page(items, number=number, total=3)
        payload["page_info"]["total_number"] = 101
        wire[1].append(payload)
        result = refresh(scene_env, redis_client, "minis", first_id)
        if number < 3:
            first_id = result.evidence_id
            assert result.next_page == number + 1
    assert result.reason_codes == ("scene_pagination_changed",)
    with Session(engine) as session:
        state = session.exec(select(SceneReadState)).one()
        assert "item_id_hashes" not in state.facts
        evidence = session.exec(select(SceneEvidence)).all()
        assert len(evidence) == 2
        assert all(len(row.facts["item_id_hashes"]) == 50 for row in evidence)
        assert all(
            len(value) == 64
            for row in evidence
            for value in row.facts["item_id_hashes"]
        )
    # Same IDs in a new generation are normal, not false duplicate detections.
    wire[1].append(page([{"minis_id": "unselected-1-0"}]))
    assert refresh(scene_env, redis_client, "minis").complete
