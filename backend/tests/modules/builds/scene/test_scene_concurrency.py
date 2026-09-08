"""Real PostgreSQL claims; official SDK's offline transport blocks at exact I/O."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.admission import admission_keys
from app.modules.accounts.models import TikTokConnection
from app.modules.builds import scene
from app.modules.builds.scene_models import SceneEvidence, SceneReadState
from app.modules.tenants.models import TenantMembership
from tests.modules.builds.scene.test_scene_reads import (
    page,
    read,
    refresh,
    role_page,
)
from tests.modules.builds.scene.test_scene_reads import (
    scene_env as scene_env,
)
from tests.modules.builds.scene.test_scene_reads import (
    source_env as source_env,
)
from tests.modules.builds.scene.test_scene_reads import (
    wire as wire,
)

BOUNDED_GUARD = scene._require_bounded_worker


def test_concurrent_refresh_commits_claim_before_network_and_releases_admission(
    scene_env, wire, redis_client
):
    entered, release = Event(), Event()

    def response():
        with Session(engine) as other, other.begin():
            # Both rows can be locked NOWAIT while official SDK is at transport.
            state = other.exec(
                select(SceneReadState).with_for_update(nowait=True)
            ).one()
            assert state.attempt_token and state.claimed_until
            other.exec(
                select(TikTokConnection)
                .where(TikTokConnection.id == scene_env["connection_id"])
                .with_for_update(nowait=True)
            ).one()
        entered.set()
        assert release.wait(5)
        return role_page()

    wire[1].append(response)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(refresh, scene_env, redis_client, "account_roles")
        try:
            assert entered.wait(5)
            duplicate = refresh(scene_env, redis_client, "account_roles")
            assert duplicate.reason_codes == ("scene_refresh_in_progress",)
            assert len(wire[0]) == 1
            keys = admission_keys(
                settings.TIKTOK_APP_ID,
                scene.api.ENDPOINTS["account_roles"],
                scene_env["context"].tenant_id,
                "actual-account",
            )
            assert any(redis_client.zcard(key) for key in keys[2:])
        finally:
            release.set()
        assert pending.result(timeout=5).complete
    assert all(redis_client.zcard(key) == 0 for key in keys[2:])
    with Session(engine) as session:
        assert len(session.exec(select(SceneEvidence)).all()) == 1


@pytest.mark.parametrize("change", ["attempt", "credentials", "membership", "deadline"])
def test_stale_remote_receipt_never_publishes(scene_env, wire, redis_client, change):
    foreign_attempt = uuid4()

    def response():
        with Session(engine) as other, other.begin():
            if change == "credentials":
                connection = other.get(TikTokConnection, scene_env["connection_id"])
                connection.credential_version += 1
            elif change == "membership":
                member = other.exec(
                    select(TenantMembership).where(
                        TenantMembership.tenant_id == scene_env["context"].tenant_id,
                        TenantMembership.user_id == scene_env["context"].actor_id,
                    )
                ).one()
                member.role = "viewer"
            else:
                state = other.exec(select(SceneReadState)).one()
                if change == "attempt":
                    state.attempt_token = foreign_attempt
                else:
                    state.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        return role_page()

    wire[1].append(response)
    result = refresh(scene_env, redis_client, "account_roles")
    assert not result.complete and result.reason_codes
    with Session(engine) as session:
        assert not session.exec(select(SceneEvidence)).all()
        state = session.exec(select(SceneReadState)).one()
        assert not state.complete
        if change == "attempt":
            assert state.attempt_token == foreign_attempt
    assert "account_build_unverified" in read(scene_env).reason_codes


def test_dead_claim_is_reclaimable_and_stale_page_pointer_is_rejected(
    scene_env, wire, redis_client
):
    wire[1].append(page([{"minis_id": f"other-{n}"} for n in range(50)], total=2))
    first = refresh(scene_env, redis_client, "minis")
    assert first.next_page == 2
    with Session(engine) as session, session.begin():
        state = session.exec(select(SceneReadState)).one()
        state.attempt_token = uuid4()
        state.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
    wire[1].append(page([{"minis_id": "other-last"}], number=2, total=2))
    assert refresh(scene_env, redis_client, "minis", first.evidence_id).complete
    with pytest.raises(DomainError) as caught:
        refresh(scene_env, redis_client, "minis", first.evidence_id)
    assert caught.value.code == "scene_refresh_stale"
    assert len(wire[0]) == 2


def test_unbounded_worker_and_short_lease_fail_before_claim(
    scene_env, wire, redis_client, monkeypatch
):
    monkeypatch.setattr(scene, "_require_bounded_worker", BOUNDED_GUARD)
    with pytest.raises(DomainError) as caught:
        refresh(scene_env, redis_client, "minis")
    assert caught.value.code == "scene_worker_unbounded"
    monkeypatch.setattr(scene, "_require_bounded_worker", lambda: None)
    policy = {
        **settings.TIKTOK_CALL_POLICIES,
        "base": {**settings.TIKTOK_CALL_POLICIES["base"], "lease_ms": 50000},
    }
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", policy)
    with pytest.raises(DomainError) as caught:
        refresh(scene_env, redis_client, "minis")
    assert caught.value.code == "admission_policy_invalid"
    with Session(engine) as session:
        assert not session.exec(select(SceneReadState)).all()
    assert not wire[0]
