"""Synthetic tenant constraints and the immutable orchestration result."""

import importlib.util

from tests.modules.builds.scene.test_scene_reads import (
    scene_env as scene_env,
)
from tests.modules.materials.test_source_uploads import (
    source_env as source_env,
)


def test_shared_scene_job_schema_exists():
    assert importlib.util.find_spec("app.modules.builds.scene_job_models"), (
        "Persistent shared scene jobs are missing"
    )


def test_scene_preparation_result_is_immutable():
    from dataclasses import FrozenInstanceError

    import pytest

    from app.modules.builds import scene_schemas

    assert hasattr(scene_schemas, "ScenePreparation"), (
        "Public preparation result is missing"
    )
    result = scene_schemas.ScenePreparation(
        job_id=None, state="blocked", reason_code="minis_unavailable"
    )
    with pytest.raises(FrozenInstanceError):
        result.state = "ready"


def test_shared_scene_tables_are_migrated():
    from sqlalchemy import text

    from app.core.db import engine

    with engine.connect() as conn:
        for name in (
            "build_scene_job",
            "build_scene_job_page",
            "draft_scene_preparation",
            "draft_capability_dependency",
            "draft_scene_dependency",
        ):
            assert (
                conn.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar()
                is not None
            )


def make_job(session, env):
    from sqlmodel import select

    from app.modules.builds.scene_job_models import SceneJob
    from app.modules.providers.models import PromotionLink, ProviderApplication

    link = session.get(PromotionLink, env["link_id"])
    app = session.exec(
        select(ProviderApplication).where(
            ProviderApplication.connection_id == link.connection_id
        )
    ).one()
    return SceneJob(
        tenant_id=env["context"].tenant_id,
        actor_id=env["context"].actor_id,
        bc_id=env["bc_id"],
        advertiser_id="actual-account",
        connection_id=env["connection_id"],
        credential_revision=0,
        provider_connection_id=link.connection_id,
        application_id=app.external_id,
        minis_id=app.tiktok_minis_id,
        scope_basis="a" * 64,
    )


def test_one_active_shared_scope_retains_completed_history(scene_env):
    import pytest
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session

    from app.core.db import engine

    with Session(engine) as session, session.begin():
        first = make_job(session, scene_env)
        session.add(first)
        session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(make_job(session, scene_env))
            session.flush()
        first.status = "COMPLETE"
        session.add(first)
        session.flush()
        next_job = make_job(session, scene_env)
        session.add(next_job)
        session.flush()
        assert first.id != next_job.id


def test_page_composite_tenant_fk_and_resource_constraint(scene_env):
    from datetime import UTC, datetime
    from uuid import uuid4

    import pytest
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.builds.scene_job_models import SceneJobPage

    with Session(engine) as session, session.begin():
        job = make_job(session, scene_env)
        session.add(job)
        session.flush()
        values = {
            "job_id": job.id,
            "resource": "identity",
            "page": 1,
            "endpoint": "/fixture",
            "source_revision": "fixture",
            "scope_basis": job.scope_basis,
            "observed_at": datetime.now(UTC),
        }
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(SceneJobPage(tenant_id=uuid4(), **values))
            session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                SceneJobPage(
                    tenant_id=job.tenant_id, **(values | {"resource": "account_roles"})
                )
            )
            session.flush()
        session.add(SceneJobPage(tenant_id=job.tenant_id, **values))
        session.flush()


def test_claim_token_and_deadline_are_paired(scene_env):
    from uuid import uuid4

    import pytest
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session

    from app.core.db import engine

    with Session(engine) as session, session.begin():
        job = make_job(session, scene_env)
        job.claim_token = uuid4()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(job)
            session.flush()
