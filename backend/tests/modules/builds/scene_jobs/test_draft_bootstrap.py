from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts.capability_models import CapabilityJob
from app.modules.builds.drafts import continue_draft, create_draft, prepare_draft
from app.modules.builds.models import BuildDraft, DraftAccount, DraftPreparation
from app.modules.builds.scene_job_models import SceneJob
from app.modules.providers.models import PromotionLink
from app.modules.strategies.models import StrategyVersion
from app.modules.strategies.service import create_strategy
from tests.modules.accounts.capabilities.test_service import page as role_page
from tests.modules.accounts.capabilities.test_service import run as run_capability
from tests.modules.builds.scene.test_scene_reads import scene_env as scene_env
from tests.modules.builds.scene_jobs.test_service import (
    job_env as job_env,
)
from tests.modules.builds.scene_jobs.test_service import (
    responses,
    run,
)
from tests.modules.builds.test_drafts import ready_links
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.strategies.test_versions import config


@pytest.fixture
def source_env(monkeypatch, redis_client, isolated_strategy_database):
    # Immutable strategy history lives in a disposable DB, never disabled in app code.
    import sys

    from tests.modules.accounts.capabilities import test_service as capability_test
    from tests.modules.builds.scene import test_scene_reads as scene_test
    from tests.modules.builds.scene_jobs import test_service as scene_service_test
    from tests.modules.materials import test_source_uploads as source_test

    database_engine, _, _ = isolated_strategy_database
    for module in (
        source_test,
        scene_test,
        scene_service_test,
        capability_test,
        sys.modules[__name__],
    ):
        monkeypatch.setattr(module, "engine", database_engine)
    generator = source_test.source_env.__wrapped__(monkeypatch, redis_client)
    env = next(generator)
    try:
        yield env
    finally:
        # Only this per-test database. TRUNCATE avoids mutating immutable history;
        # the owning fixture drops the entire database immediately afterwards.
        with database_engine.begin() as connection:
            connection.execute(text("TRUNCATE strategy_version CASCADE"))
        with pytest.raises(StopIteration):
            next(generator)


def seed_draft(env, account_ids=None):
    with Session(engine) as session, session.begin():
        link = session.get(PromotionLink, env["link_id"])
        strategy = create_strategy(
            session,
            context=env["context"],
            name="Scene preparation fixture",
            config=config(),
        )
        version = session.exec(
            select(StrategyVersion).where(StrategyVersion.strategy_id == strategy)
        ).one()
        intent = {
            "bc_id": env["bc_id"],
            "strategy_version_id": version.id,
            "provider_connection_id": link.connection_id,
            "application_id": link.application_id,
            "drama_lines": ["Moon"],
            "account_lines": account_ids or ["actual-account"],
            "link_config": {"episode": 1},
        }
        draft_id = create_draft(session, context=env["context"], **intent)
        task_id = prepare_draft(
            session, context=env["context"], draft_id=draft_id, request_id=uuid4()
        )
        ready_links(session, env["context"], task_id, intent)
        from app.modules.builds.mini_targets import remember_target

        remember_target(
            session,
            context=env["context"],
            url="https://example.com/drama",
            minis_id="fixture-minis",
            source="USER",
        )
        return draft_id, task_id


def tick(env, task_id):
    with Session(engine) as session, session.begin():
        return continue_draft(session, context=env["context"], task_id=task_id)


def test_draft_freezes_before_worker_and_queues_original_connection_without_default(
    job_env,
):
    from app.modules.accounts.connection_models import BCDefaultRoute
    from app.modules.builds.scene_job_models import (
        DraftCapabilityDependency,
        DraftScenePreparation,
    )

    env = job_env
    _, task_id = seed_draft(env)
    with Session(engine) as db, db.begin():
        state = db.get(DraftScenePreparation, (env["context"].tenant_id, task_id))
        assert state.frozen_route == env["route"].model_dump(mode="json")
        db.delete(db.get(BCDefaultRoute, (env["context"].tenant_id, env["bc_id"])))
    assert not tick(env, task_id)
    with Session(engine) as db:
        dependencies = db.exec(
            select(DraftCapabilityDependency).where(
                DraftCapabilityDependency.preparation_id == task_id
            )
        ).all()
        assert len(dependencies) == 1
        assert dependencies[0].connection_id == env["route"].connection_id
        assert db.get(
            DraftScenePreparation, (env["context"].tenant_id, task_id)
        ).frozen_route == env["route"].model_dump(mode="json")


def test_unknown_accounts_bootstrap_before_strict_resolution_then_wait_for_shared_scene(
    job_env, wire, redis_client
):
    env = job_env
    draft_id, task_id = seed_draft(env)
    assert not tick(env, task_id)
    with Session(engine) as session:
        cap = session.exec(
            select(CapabilityJob).where(
                CapabilityJob.tenant_id == env["context"].tenant_id
            )
        ).one_or_none()
        assert cap is not None, (
            "Draft prepare never bootstrapped UNKNOWN account capabilities"
        )
        assert not session.exec(
            select(DraftAccount).where(DraftAccount.draft_id == draft_id)
        ).all()
    wire[1].append(role_page(["actual-account"]))
    run_capability(env, redis_client, cap.id)
    run_capability(env, redis_client, cap.id)
    for _ in range(12):
        tick(env, task_id)
        with Session(engine) as session:
            scene = session.exec(
                select(SceneJob).where(SceneJob.tenant_id == env["context"].tenant_id)
            ).first()
            if scene is not None:
                break
    assert scene is not None, "Prepared assets never scheduled shared scene reads"
    with Session(engine) as session:
        assert session.get(BuildDraft, draft_id).status == "PREPARING"
        assert (
            session.exec(select(DraftAccount).where(DraftAccount.draft_id == draft_id))
            .one()
            .advertiser_id
            == "actual-account"
        )
    wire[1].extend(responses(env))
    for _ in range(6):
        scene = run(env, redis_client, scene.id)
        if scene.status != "PENDING":
            break
    assert scene.status == "COMPLETE"
    for _ in range(5):
        if tick(env, task_id):
            break
    with Session(engine) as session:
        assert session.get(BuildDraft, draft_id).status == "READY"
        assert session.get(DraftPreparation, task_id).status == "READY"
    assert len(wire[0]) == 6


@pytest.mark.parametrize(
    "scope,role,reason",
    [
        (None, "OPERATOR", "capability_scope_unknown"),
        ("[2,6]", "ANALYST", "account_build_unverified"),
    ],
)
def test_unknown_scope_or_readonly_role_finishes_with_explicit_account_block(
    job_env, wire, redis_client, scope, role, reason
):
    from app.core.credentials import encrypt_credentials
    from app.modules.accounts.models import TikTokConnection
    from app.modules.builds.models import DraftInput

    env = job_env
    with Session(engine) as session, session.begin():
        conn = session.get(TikTokConnection, env["connection_id"])
        credentials = {"access_token": "offline-token"}
        if scope is not None:
            credentials["scope"] = scope
        conn.credential_ciphertext = encrypt_credentials(
            tenant_id=conn.tenant_id, value=credentials
        )
        if scope is None:
            from app.modules.accounts.connection_models import ConnectionAuthorization

            authorization = session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == conn.id
                )
            ).one()
            authorization.scopes = []
            authorization.permission_summary = {
                "read_authorized": True,
                "build_authorized": None,
                "upload_authorized": None,
            }
    draft_id, task_id = seed_draft(env)
    tick(env, task_id)
    with Session(engine) as session:
        cap = session.exec(
            select(CapabilityJob).where(
                CapabilityJob.tenant_id == env["context"].tenant_id
            )
        ).one()
    wire[1].append(role_page(["actual-account"], role=role))
    run_capability(env, redis_client, cap.id)
    run_capability(env, redis_client, cap.id)
    for _ in range(12):
        if tick(env, task_id):
            break
    with Session(engine) as session:
        assert session.get(BuildDraft, draft_id).status == "READY"
        line = session.exec(
            select(DraftInput).where(
                DraftInput.draft_id == draft_id, DraftInput.kind == "account"
            )
        ).one()
        assert line.status == "blocked" and line.reason_code == reason
        assert not session.exec(
            select(DraftAccount).where(DraftAccount.draft_id == draft_id)
        ).all()
        assert not session.exec(
            select(SceneJob).where(SceneJob.tenant_id == env["context"].tenant_id)
        ).all()
        assert session.get(TikTokConnection, env["connection_id"]).status == "ACTIVE"
    assert len(wire[0]) == 1


def test_205_accounts_use_one_bc_chain_and_bounded_persisted_scene_cursor(
    job_env, wire, redis_client
):
    from sqlalchemy import func

    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.builds.scene_job_models import (
        DraftSceneDependency,
        DraftScenePreparation,
    )

    env = job_env
    ids = ["actual-account", *[f"account-{i:04}" for i in range(204)]]
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                AdvertiserAccount(
                    tenant_id=env["context"].tenant_id,
                    advertiser_id=identity,
                    currency="USD",
                    timezone="UTC",
                    remote_status="ENABLE",
                )
                for identity in ids[1:]
            ]
        )
        session.flush()
        session.add_all(
            [
                BCAccountAccess(
                    tenant_id=env["context"].tenant_id,
                    bc_id=env["bc_id"],
                    advertiser_id=identity,
                    connection_id=env["connection_id"],
                    active=True,
                    authorized=True,
                    in_bc=True,
                )
                for identity in ids[1:]
            ]
        )
    draft_id, task_id = seed_draft(env, ids)
    tick(env, task_id)
    with Session(engine) as session:
        cap = session.exec(
            select(CapabilityJob).where(
                CapabilityJob.tenant_id == env["context"].tenant_id
            )
        ).one()
    for number in range(5):
        wire[1].append(role_page(ids[number * 50 : (number + 1) * 50], number + 1, 205))
        run_capability(env, redis_client, cap.id)
    for _ in range(3):
        run_capability(env, redis_client, cap.id)
    previous = 0
    counts = []
    for _ in range(18):
        tick(env, task_id)
        with Session(engine) as session:
            count = session.exec(
                select(func.count())
                .select_from(DraftSceneDependency)
                .where(DraftSceneDependency.preparation_id == task_id)
            ).one()
            assert 0 <= count - previous <= 100
            if count != previous:
                counts.append(count)
            previous = count
            if count == 205:
                control = session.get(
                    DraftScenePreparation, (env["context"].tenant_id, task_id)
                )
                assert control.scenes_queued and control.scene_after == max(ids)
                break
    assert counts == [100, 200, 205]
    with Session(engine) as session:
        assert (
            session.exec(
                select(func.count())
                .select_from(CapabilityJob)
                .where(CapabilityJob.tenant_id == env["context"].tenant_id)
            ).one()
            == 1
        )
        assert (
            session.exec(
                select(func.count())
                .select_from(SceneJob)
                .where(SceneJob.tenant_id == env["context"].tenant_id)
            ).one()
            == 205
        )
        assert session.get(BuildDraft, draft_id).status == "PREPARING"
    assert len(wire[0]) == 5


def selection_draft(env, wire, redis_client):
    from app.modules.builds.mini_targets import MiniTarget, url_key
    from tests.modules.builds.scene_jobs.test_service import complete

    catalog = complete(env, wire, redis_client)
    draft_id, task_id = seed_draft(env)
    for _ in range(20):
        if tick(env, task_id):
            break
    with Session(engine) as session, session.begin():
        draft = session.get(BuildDraft, draft_id)
        assert draft.status == "READY"
        session.delete(
            session.get(
                MiniTarget,
                (env["context"].tenant_id, url_key("https://example.com/drama")),
            )
        )
        return draft_id, draft.revision, catalog


def test_name_choice_persists_exact_links_and_recovers_same_request(
    job_env, wire, redis_client
):
    from app.core.errors import DomainError
    from app.modules.builds.mini_selection import (
        ChooseMiniRequest,
        choose_mini,
        draft_minis,
    )
    from app.modules.builds.mini_targets import MiniTarget, url_key
    from app.modules.providers.models import ProviderApplication

    env = job_env
    draft_id, revision, catalog = selection_draft(env, wire, redis_client)
    with Session(engine) as db, db.begin():
        db.exec(
            select(ProviderApplication).where(
                ProviderApplication.tenant_id == env["context"].tenant_id
            )
        ).one().tiktok_minis_id = None
        options = draft_minis(db, context=env["context"], draft_id=draft_id)
        assert options.state == "choose" and options.selected is None
        assert options.items[0].minis_id == "fixture-minis"
    body = ChooseMiniRequest(
        request_id=uuid4(),
        expected_revision=revision,
        catalog_job_id=catalog.id,
        minis_id="fixture-minis",
    )
    for _ in range(2):
        with Session(engine) as db, db.begin():
            assert (
                choose_mini(db, context=env["context"], draft_id=draft_id, body=body)
                == revision + 1
            )
    with Session(engine) as db:
        target = db.get(
            MiniTarget, (env["context"].tenant_id, url_key("https://example.com/drama"))
        )
        assert (target.minis_id, target.source, target.actor_id) == (
            "fixture-minis",
            "USER",
            env["context"].actor_id,
        )
        assert (
            draft_minis(db, context=env["context"], draft_id=draft_id).state
            == "selected"
        )
    with pytest.raises(DomainError) as raised, Session(engine) as db, db.begin():
        choose_mini(
            db,
            context=env["context"],
            draft_id=draft_id,
            body=body.model_copy(update={"minis_id": "another"}),
        )
    assert raised.value.code == "idempotency_conflict"
    assert len(wire[0]) == 6  # 查看和选择目录均不新增 TikTok 请求。


@pytest.mark.parametrize(
    "failure",
    ["revision", "stale", "unavailable", "link_query", "link_path", "viewer", "tenant"],
)
def test_mini_choice_rejects_stale_or_wrong_target_without_saving(
    job_env, wire, redis_client, failure
):
    from datetime import UTC, datetime, timedelta

    from app.core.errors import DomainError
    from app.modules.builds.mini_selection import ChooseMiniRequest, choose_mini
    from app.modules.builds.mini_targets import MiniTarget, url_key
    from app.modules.builds.models import DraftDrama
    from app.modules.builds.scene_job_models import SceneJobPage
    from app.modules.tenants.models import TenantMembership

    env = job_env
    selection_context = env["context"]
    draft_id, revision, catalog = selection_draft(env, wire, redis_client)
    body = ChooseMiniRequest(
        request_id=uuid4(),
        expected_revision=revision,
        catalog_job_id=catalog.id,
        minis_id="fixture-minis",
    )
    with Session(engine) as db, db.begin():
        if failure == "tenant":
            from tests.modules.conftest import create_context

            selection_context = create_context(db)
        elif failure == "revision":
            body.expected_revision += 1
        elif failure == "stale":
            db.get(SceneJob, catalog.id).expires_at = datetime.now(UTC) - timedelta(
                seconds=1
            )
        elif failure == "unavailable":
            body.minis_id = "not-an-account-mini"
        elif failure.startswith("link_"):
            row = db.exec(
                select(DraftDrama).where(DraftDrama.draft_id == draft_id)
            ).one()
            db.get(PromotionLink, row.link_id).url = (
                "https://www.tiktok.com/minis/other-mini"
                + ("?minis_id=other-mini" if failure == "link_query" else "")
            )
            page = db.exec(
                select(SceneJobPage).where(
                    SceneJobPage.job_id == catalog.id, SceneJobPage.resource == "minis"
                )
            ).one()
            page.facts = {
                **page.facts,
                "options": [
                    *page.facts["options"],
                    {
                        "minis_id": "other-mini",
                        "name": "Other",
                        "status": "ACTIVE",
                        "type": "MINI_SERIES",
                        "regions": ["US"],
                    },
                ],
            }
        elif failure == "viewer":
            db.get(
                TenantMembership, (env["context"].tenant_id, env["context"].actor_id)
            ).role = "viewer"
    with pytest.raises(DomainError) as raised, Session(engine) as db, db.begin():
        choose_mini(db, context=selection_context, draft_id=draft_id, body=body)
    assert (
        raised.value.code
        == {
            "revision": "draft_revision_conflict",
            "stale": "minis_catalog_stale",
            "unavailable": "minis_unavailable",
            "link_query": "minis_link_conflict",
            "link_path": "minis_link_conflict",
            "viewer": "action_forbidden",
            "tenant": "draft_not_found",
        }[failure]
    )
    with Session(engine) as db:
        assert db.get(BuildDraft, draft_id).revision == revision
        assert (
            db.get(
                MiniTarget,
                (env["context"].tenant_id, url_key("https://example.com/drama")),
            )
            is None
        )
