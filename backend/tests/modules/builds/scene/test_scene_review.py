"""Independent review: repeated rows cannot establish a complete remote list."""

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import BCAccountAccess
from app.modules.builds import scene
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
        wire[1].append(page([unavailable[0]], key="identity_list", number=2, total=2))
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


def test_scene_read_in_postgres_read_only_transaction_uses_no_credentials_or_io(
    scene_env, wire, monkeypatch
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Local Scene read attempted external work")

    for name in ("admitted_account_call", "sdk_client", "decrypt_credentials"):
        monkeypatch.setattr(scene, name, forbidden)
    monkeypatch.setattr(scene.api, "request_page", forbidden)
    statements = []

    def track(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lstrip().split()[0].upper())

    with Session(engine) as session:
        before_dispatches = session.exec(select(PendingDispatch)).all()
        event.listen(engine, "before_cursor_execute", track)
        try:
            session.rollback()
            session.execute(text("SET TRANSACTION READ ONLY"))
            result = scene.read_scene_context(
                session,
                context=scene_env["context"],
                bc_id=scene_env["bc_id"],
                advertiser_id="actual-account",
                link_id=scene_env["link_id"],
            )
            assert not result.supported
            assert "account_scope_unverified" in result.reason_codes
            assert len(session.exec(select(PendingDispatch)).all()) == len(
                before_dispatches
            )
            grant = session.exec(select(BCAccountAccess)).one()
            assert grant.permission_state == "UNKNOWN"
            assert not grant.can_build and not grant.can_upload
            session.commit()
        finally:
            event.remove(engine, "before_cursor_execute", track)
    assert set(statements) == {"SET", "SELECT"}
    assert not wire[0]


@pytest.mark.parametrize("resource", ["account_roles", "minis", "identity"])
def test_full_205_row_remote_list_requires_all_five_bounded_pages(
    scene_env, wire, redis_client, resource
):
    previous = None
    for number in range(1, 6):
        values = []
        for offset in range((number - 1) * 50, min(number * 50, 205)):
            if resource == "identity":
                value = {
                    **identity(scene_env["bc_id"], str(offset)),
                    "can_push_video": offset == 204,
                }
            elif resource == "minis":
                value = {"minis_id": f"other-{offset}"}
                if offset == 204:
                    value = {
                        "minis_id": "fixture-minis",
                        "minis_status": "ACTIVE",
                        "minis_type": "MINI_SERIES",
                        "region_codes": ["US"],
                    }
            else:
                value = {
                    "asset_id": "actual-account"
                    if offset == 204
                    else f"other-{offset}",
                    "asset_type": "ADVERTISER",
                    "advertiser_role": "OPERATOR",
                }
            values.append(value)
        response = page(
            values,
            key="identity_list" if resource == "identity" else "list",
            number=number,
            total=5,
        )
        response["page_info"]["total_number"] = 205
        wire[1].append(response)
        result = refresh(scene_env, redis_client, resource, previous)
        assert result.complete == (number == 5)
        assert result.next_page == (number + 1 if number < 5 else None)
        previous = result.evidence_id
        assert previous
    with Session(engine) as session:
        state = session.exec(select(SceneReadState)).one()
        assert state.complete and state.facts["seen"] == 205
        assert len(state.facts["matches"]) == 1
    assert len(wire[0]) == 5
