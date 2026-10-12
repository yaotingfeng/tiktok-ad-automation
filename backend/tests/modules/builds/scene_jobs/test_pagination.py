"""Complete shared facts require every distinct remote row, including nonmatches."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.builds.scene_job_models import SceneJob, SceneJobPage
from tests.modules.builds.scene.test_scene_reads import page
from tests.modules.builds.scene.test_scene_reads import scene_env as scene_env
from tests.modules.builds.scene_jobs.test_recovery import ready_for_get
from tests.modules.builds.scene_jobs.test_service import ensure, responses, run
from tests.modules.builds.scene_jobs.test_service import job_env as job_env
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def test_205_identities_and_minis_require_complete_identity_and_five_mini_pages(
    job_env, wire, redis_client
):
    env = job_env
    job = ready_for_get(env, wire, redis_client)
    expected = responses(env)
    for resource, key, field, selected in (
        ("identity", "identity_list", "identity_id", expected[0]["identity_list"][0]),
        ("minis", "list", "minis_id", expected[1]["list"][0]),
    ):
        for number in (range(1, 2) if resource == "identity" else range(1, 6)):
            values = [
                {field: f"unselected-{n}"}
                for n in (range(205) if resource == "identity" else range((number - 1) * 50, min(number * 50, 205)))
            ]
            if resource == "identity":
                values = [
                    {
                        **item,
                        "identity_type": "BC_AUTH_TT",
                        "identity_authorized_bc_id": env["bc_id"],
                        "can_push_video": False,
                    }
                    for item in values
                ]
            if number == 1:
                values[0] = selected
            if resource == "minis" and number == 5:
                values[-1] = {
                    **selected,
                    "minis_id": "last-page-mini",
                    "minis_name": "Last page",
                }
            payload = page(values, key=key, number=number, total=5)
            payload["page_info"]["total_number"] = 205
            if resource == "identity":
                payload["page_info"] = dict.fromkeys(payload["page_info"], 0)
            wire[1].append(payload)
            job = run(env, redis_client, job.id)
            assert job.status == "PENDING"
            assert ensure(env).state == "queued"
            assert "item_id_hashes" not in job.facts[resource]
    wire[1].extend(expected[2:])
    for _ in range(4):
        job = run(env, redis_client, job.id)
    assert job.status == "COMPLETE" and ensure(env).state == "ready"
    with Session(engine) as session:
        pages = session.exec(
            select(SceneJobPage).where(SceneJobPage.job_id == job.id)
        ).all()
        assert len(pages) == 9
        from app.modules.builds.mini_selection import catalog_options

        assert catalog_options(session, job, page=5)[0]["name"] == "Last page"
        assert (
            catalog_options(session, job, minis_id="last-page-mini")[0]["name"]
            == "Last page"
        )
        assert all(
            len(p.facts.get("item_id_hashes", [])) <= 50
            for p in pages if p.resource != "identity"
        )
    assert len(wire[0]) == 10


@pytest.mark.parametrize(
    "resource,key,field",
    [("minis", "list", "minis_id")],
)
def test_duplicate_in_any_earlier_page_keeps_shared_job_failed(
    job_env, wire, redis_client, resource, key, field
):
    env = job_env
    job = ready_for_get(env, wire, redis_client)
    if resource == "minis":
        wire[1].append(responses(env)[0])
        job = run(env, redis_client, job.id)
    for number in (1, 2, 3):
        values = (
            [{field: f"{number}-{n}"} for n in range(50)]
            if number < 3
            else [{field: "1-0"}]
        )
        if resource == "identity":
            values = [
                {
                    **item,
                    "identity_type": "BC_AUTH_TT",
                    "identity_authorized_bc_id": env["bc_id"],
                    "can_push_video": False,
                }
                for item in values
            ]
        payload = page(values, key=key, number=number, total=3)
        payload["page_info"]["total_number"] = 101
        wire[1].append(payload)
        job = run(env, redis_client, job.id)
    assert job.status == "FAILED" and job.error_code == "scene_pagination_changed"
    assert ensure(env).state == "blocked"
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(SceneJobPage).where(
                        SceneJobPage.job_id == job.id, SceneJobPage.resource == resource
                    )
                ).all()
            )
            == 2
        )


def test_scene_ttl_keeps_first_observation_and_does_not_extend_at_completion(
    job_env, wire, redis_client
):
    env = job_env
    job = ready_for_get(env, wire, redis_client)
    wire[1].append(responses(env)[0])
    job = run(env, redis_client, job.id)
    first, expiry = job.first_observed_at, job.expires_at
    wire[1].extend(responses(env)[1:])
    for _ in range(5):
        job = run(env, redis_client, job.id)
    assert job.first_observed_at == first and job.expires_at == expiry
    assert job.completed_at > first
    with Session(engine) as session, session.begin():
        session.get(SceneJob, job.id).expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    renewed = ensure(env)
    assert renewed.state == "queued" and renewed.job_id != job.id
    with Session(engine) as session:
        assert session.get(SceneJob, renewed.job_id).first_observed_at is None


def test_partial_paginated_identity_response_is_rejected(job_env, wire, redis_client):
    env = job_env
    job = ready_for_get(env, wire, redis_client)
    payload = responses(env)[0]
    payload["page_info"].update(total_page=2, total_number=51)
    wire[1].append(payload)
    job = run(env, redis_client, job.id)
    assert job.status == "FAILED"
    assert job.error_code == "scene_response_unverified"
    with Session(engine) as session:
        assert session.exec(
            select(SceneJobPage).where(SceneJobPage.job_id == job.id)
        ).first() is None
