"""Real committed jobs, official SDK transport doubles, no live API."""

import importlib.util
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts import capabilities
from app.modules.builds.scene_job_models import SceneJob, SceneJobPage
from app.modules.providers.models import PromotionLink
from tests.modules.accounts.capabilities.test_service import page as role_page
from tests.modules.accounts.capabilities.test_service import run as run_capability
from tests.modules.builds.scene.test_scene_reads import page
from tests.modules.builds.scene.test_scene_reads import scene_env as scene_env
from tests.modules.builds.scene_jobs.test_policy_regions import region_response
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


@pytest.fixture
def job_env(scene_env, monkeypatch):
    assert importlib.util.find_spec("app.modules.builds.scene_jobs"), (
        "Shared scene orchestration is missing"
    )
    from app.modules.builds import scene_jobs

    monkeypatch.setattr(scene_jobs, "_require_bounded_worker", lambda: None)
    monkeypatch.setattr(capabilities, "_require_bounded_worker", lambda: None)
    return scene_env


def ensure(env, link_id=None):
    from app.modules.builds.scene_jobs import ensure_scene_preparation

    with Session(engine) as session, session.begin():
        return ensure_scene_preparation(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=link_id or env["link_id"],
        )


def run(env, redis_client, job_id):
    from app.modules.builds.scene_jobs import process_scene_job

    with Session(engine) as session, session.begin():
        job = session.get(SceneJob, job_id)
        job.due_at = datetime.now(UTC) - timedelta(seconds=1)
        revision = job.revision
    process_scene_job(
        database_engine=engine,
        redis_client=redis_client,
        tenant_id=env["context"].tenant_id,
        actor_id=env["context"].actor_id,
        payload={"job_id": str(job_id), "revision": revision},
    )
    with Session(engine) as session:
        return session.get(SceneJob, job_id)


def responses(env):
    return [
        page(
            [
                {
                    "identity_id": "fixture-identity",
                    "identity_type": "BC_AUTH_TT",
                    "identity_authorized_bc_id": env["bc_id"],
                    "available_status": "AVAILABLE",
                    "can_push_video": True,
                    "is_gpppa": False,
                }
            ],
            key="identity_list",
        ),
        page(
            [
                {
                    "minis_id": "fixture-minis",
                    "minis_status": "ACTIVE",
                    "minis_type": "MINI_SERIES",
                    "region_codes": ["US", "CA"],
                }
            ]
        ),
        {
            "recommend_assets": [
                {"asset_ids": ["cta-watch"], "asset_content": "Watch now"}
            ]
        },
        {"vo_min_roas": "QUALIFIED"},
        region_response()["data"],
    ]


def complete(env, wire, redis_client):
    receipt = ensure(env)
    job = run(env, redis_client, receipt.job_id)
    assert job.capability_job_id
    wire[1].append(role_page(["actual-account"]))
    cap_env = {**env, "request_id": uuid4()}
    run_capability(cap_env, redis_client, job.capability_job_id)
    run_capability(cap_env, redis_client, job.capability_job_id)
    wire[1].extend(responses(env))
    for _ in range(8):
        job = run(env, redis_client, job.id)
        if job.status != "PENDING":
            break
    assert job.status == "COMPLETE", job.error_code
    return job


def test_same_app_different_drama_links_share_complete_scene_and_bc_proof(
    job_env, wire, redis_client
):
    from app.modules.builds.scene import read_scene_context

    env = job_env
    with Session(engine) as session, session.begin():
        original = session.get(PromotionLink, env["link_id"])
        clone = PromotionLink(
            **(
                original.model_dump()
                | {
                    "id": uuid4(),
                    "reuse_key": "b" * 64,
                    "is_current": False,
                    "url": "https://example.com/another-drama",
                }
            )
        )
        session.add(clone)
        session.flush()
        other_link = clone.id
    first = ensure(env)
    second = ensure(env, other_link)
    assert first.job_id == second.job_id and first.state == second.state == "queued"
    job = complete(env, wire, redis_client)
    assert len(wire[0]) == 6  # one BC role GET, four scene assets, one targeting GET
    assert all(call[0] == "GET" for call in wire[0])
    assert ensure(env, other_link).state == "ready"
    with Session(engine) as session:
        result = read_scene_context(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=other_link,
        )
        assert result.supported, result.reason_codes
        assert result.adgroup_fields["targeting_spec"]["location_ids"] == (
            "6251999",
            "6252001",
        )
        assert result.adgroup_fields["placement_type"] == "PLACEMENT_TYPE_NORMAL"
        assert result.adgroup_fields["placements"] == ("PLACEMENT_TIKTOK",)
        assert result.copy_length_limit == 100
        assert result.field_constraints["platform_copy_length"] is None
        assert (
            len(
                session.exec(
                    select(SceneJobPage).where(SceneJobPage.job_id == job.id)
                ).all()
            )
            == 5
        )


def test_complete_scene_reader_is_readonly_no_credentials_or_network(
    job_env, wire, redis_client, monkeypatch
):
    from app.modules.builds import scene, scene_jobs

    env = job_env
    complete(env, wire, redis_client)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Pure scene read performed a side effect")

    for module, name in [
        (scene, "decrypt_credentials"),
        (scene_jobs, "sdk_client"),
        (scene_jobs, "enqueue_after_commit"),
        (capabilities, "decrypt_credentials"),
    ]:
        monkeypatch.setattr(module, name, forbidden)
    statements = []

    def capture(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement.lstrip().split()[0])

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            result = scene.read_scene_context(
                session,
                context=env["context"],
                bc_id=env["bc_id"],
                advertiser_id="actual-account",
                link_id=env["link_id"],
            )
            assert result.supported
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert set(statements) == {"SELECT", "SET"}


def test_partial_scene_never_reports_ready(job_env, wire):
    from app.modules.builds.scene import read_scene_context

    env = job_env
    receipt = ensure(env)
    with Session(engine) as session:
        result = read_scene_context(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            link_id=env["link_id"],
        )
        assert not result.supported and "scene_evidence_missing" in result.reason_codes
    assert receipt.state == "queued" and not wire[0]
