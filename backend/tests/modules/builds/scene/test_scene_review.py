"""Independent review: repeated rows cannot establish a complete remote list."""

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.builds.scene_models import SceneReadState
from tests.modules.builds.scene.test_scene_reads import identity, page, refresh
from tests.modules.builds.scene.test_scene_reads import scene_env as scene_env
from tests.modules.builds.scene.test_scene_reads import source_env as source_env
from tests.modules.builds.scene.test_scene_reads import wire as wire


@pytest.mark.parametrize("cross_page", [False, True])
def test_repeated_identity_rows_never_complete_unique_identity_selection(
    scene_env, wire, redis_client, cross_page
):
    eligible = identity(scene_env["bc_id"], "eligible")
    unavailable = [
        {**identity(scene_env["bc_id"], str(n)), "can_push_video": False}
        for n in range(49)
    ]
    if cross_page:
        # A stable count can hide an offset shift: 51 rows received, only 50 IDs.
        wire[1].append(page([eligible, *unavailable], key="identity_list", total=2))
        first = refresh(scene_env, redis_client, "identity")
        assert first.next_page == 2 and not first.complete
        wire[1].append(
            page([unavailable[0]], key="identity_list", number=2, total=2)
        )
        result = refresh(scene_env, redis_client, "identity", first.evidence_id)
    else:
        wire[1].append(
            page([eligible, unavailable[0], unavailable[0]], key="identity_list")
        )
        result = refresh(scene_env, redis_client, "identity")
    assert not result.complete
    assert set(result.reason_codes) & {
        "scene_pagination_changed",
        "scene_response_unverified",
    }
    with Session(engine) as session:
        state = session.exec(
            select(SceneReadState).where(SceneReadState.resource == "identity")
        ).one()
        assert not state.complete
