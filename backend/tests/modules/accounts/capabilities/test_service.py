"""Capability bootstrap uses only BC/token scope, never a provider link."""

import importlib.util
from uuid import uuid4

import pytest
from sqlmodel import Session, col, select

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.accounts.capability_models import CapabilityJob, CapabilityRequest
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TikTokConnection,
)


def test_capability_service_exists():
    assert importlib.util.find_spec("app.modules.accounts.capabilities"), (
        "Link-free bootstrap service missing"
    )


def start(env):
    from app.modules.accounts.capabilities import start_capability_refresh

    with Session(engine) as session, session.begin():
        return start_capability_refresh(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            connection_id=env["connection_id"],
            request_id=env["request_id"],
        )


def run(env, redis_client, job_id):
    from app.modules.accounts.capabilities import process_capability

    with Session(engine) as session:
        job = session.get(CapabilityJob, job_id)
        revision = job.revision
    process_capability(
        database_engine=engine,
        redis_client=redis_client,
        tenant_id=env["context"].tenant_id,
        actor_id=env["context"].actor_id,
        payload={"job_id": str(job_id), "revision": revision},
    )
    with Session(engine) as session:
        return session.get(CapabilityJob, job_id)


def page(ids, number=1, count=None, role="OPERATOR"):
    total = len(ids) if count is None else count
    return {
        "list": [
            {"asset_id": i, "asset_type": "ADVERTISER", "advertiser_role": role}
            for i in ids
        ],
        "page_info": {
            "page": number,
            "page_size": 50,
            "total_page": (total + 49) // 50,
            "total_number": total,
        },
    }


def test_start_is_flush_only_idempotent_and_reuses_active_connection_job(
    capability_env, wire
):
    from app.modules.accounts.capabilities import start_capability_refresh

    env = capability_env
    with Session(engine) as session:
        job_id = start_capability_refresh(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            connection_id=env["connection_id"],
            request_id=env["request_id"],
        )
        with Session(engine) as other:
            assert other.get(CapabilityJob, job_id) is None
        session.rollback()
    job_id = start(env)
    assert start(env) == job_id
    assert start({**env, "request_id": uuid4()}) == job_id
    with Session(engine) as session:
        assert len(session.exec(select(CapabilityJob)).all()) == 1
        assert len(session.exec(select(CapabilityRequest)).all()) == 2
        assert len(session.exec(select(PendingDispatch)).all()) == 1
    assert not wire[0]


def test_205_rows_five_reads_then_three_atomic_publish_pages(
    capability_env, wire, redis_client
):
    env = capability_env
    ids = ["actual-account"] + [f"account-{n:04}" for n in range(204)]
    with Session(engine) as session, session.begin():
        for aid in ids[1:]:
            session.add(
                AdvertiserAccount(
                    tenant_id=env["context"].tenant_id,
                    advertiser_id=aid,
                    currency="USD",
                    timezone="UTC",
                    remote_status="ENABLE",
                )
            )
        session.flush()
        for aid in ids[1:]:
            session.add(
                BCAccountAccess(
                    tenant_id=env["context"].tenant_id,
                    bc_id=env["bc_id"],
                    advertiser_id=aid,
                    connection_id=env["connection_id"],
                    in_bc=True,
                    authorized=True,
                    active=True,
                )
            )
    job_id = start(env)
    for n in range(5):
        wire[1].append(page(ids[n * 50 : (n + 1) * 50], n + 1, len(ids)))
        job = run(env, redis_client, job_id)
        with Session(engine) as session:
            assert not session.exec(
                select(BCAccountAccess).where(col(BCAccountAccess.can_build).is_(True))
            ).all()
    assert job.phase == "PUBLISH"
    for expected in [100, 200, 205]:
        job = run(env, redis_client, job_id)
        assert job.published_count == expected
    assert job.status == "COMPLETE" and job.phase == "DONE"
    assert len(wire[0]) == 5
    for method, url, kwargs in wire[0]:
        assert method == "GET" and url.endswith("/bc/asset/get/")
        fields = dict(kwargs["fields"])
        assert (
            fields["bc_id"] == env["bc_id"]
            and fields["page_size"] == 50
            and "filtering" not in fields
        )
    with Session(engine) as session:
        grants = session.exec(select(BCAccountAccess)).all()
        assert len(grants) == 205 and all(
            g.can_build and g.can_upload and g.permission_state == "VERIFIED"
            for g in grants
        )


@pytest.mark.parametrize(
    "scope,role,expected",
    [
        (None, "ADMIN", ("UNKNOWN", False, False)),
        ("[2]", "OPERATOR", ("VERIFIED", True, False)),
        ("[611]", "ADMIN", ("VERIFIED", False, True)),
        ("[2,6]", "ANALYST", ("VERIFIED", False, False)),
    ],
)
def test_scope_and_actual_user_role_are_both_required(
    capability_env, wire, redis_client, scope, role, expected
):
    from app.core.credentials import encrypt_credentials

    env = capability_env
    with Session(engine) as session, session.begin():
        conn = session.get(TikTokConnection, env["connection_id"])
        private = {"access_token": "offline-token"}
        if scope is not None:
            private["scope"] = scope
        conn.credential_ciphertext = encrypt_credentials(
            tenant_id=conn.tenant_id, value=private
        )
    job_id = start(env)
    wire[1].append(page(["actual-account"], role=role))
    run(env, redis_client, job_id)
    job = run(env, redis_client, job_id)
    assert job.status == "COMPLETE"
    with Session(engine) as session:
        grant = session.exec(select(BCAccountAccess)).one()
        assert (grant.permission_state, grant.can_build, grant.can_upload) == expected
        assert session.get(TikTokConnection, env["connection_id"]).status == "ACTIVE"


def test_repeated_remote_id_fails_without_grant_publication(
    capability_env, wire, redis_client
):
    env = capability_env
    job_id = start(env)
    wire[1].append(page(["actual-account"] + [f"other-{n}" for n in range(49)], 1, 51))
    run(env, redis_client, job_id)
    wire[1].append(page(["other-1"], 2, 51))
    result = run(env, redis_client, job_id)
    assert (
        result.status == "FAILED"
        and result.error_code == "capability_response_unverified"
    )
    with Session(engine) as session:
        assert not session.exec(select(BCAccountAccess)).one().can_build


def test_concurrent_request_id_cannot_change_connection(capability_env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.core.credentials import encrypt_credentials
    from app.modules.accounts import capabilities

    env = capability_env
    with Session(engine) as session, session.begin():
        second = TikTokConnection(
            tenant_id=env["context"].tenant_id,
            status="ACTIVE",
            credential_ciphertext=encrypt_credentials(
                tenant_id=env["context"].tenant_id,
                value={"access_token": "offline-second", "scope": "[2,6]"},
            ),
        )
        session.add(second)
        session.flush()
        second_id = second.id
        session.add(
            BCAccountAccess(
                tenant_id=env["context"].tenant_id,
                bc_id=env["bc_id"],
                advertiser_id="actual-account",
                connection_id=second.id,
                in_bc=True,
                authorized=True,
                active=True,
            )
        )
    barrier = Barrier(2)
    original = capabilities._directory_basis

    def paused(*args, **kwargs):
        result = original(*args, **kwargs)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(capabilities, "_directory_basis", paused)

    def attempt(connection_id):
        try:
            return start({**env, "connection_id": connection_id})
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [env["connection_id"], second_id]))
    assert results.count("request_id_conflict") == 1
    with Session(engine) as session:
        assert len(session.exec(select(CapabilityJob)).all()) == 1
        assert len(session.exec(select(PendingDispatch)).all()) == 1


def test_two_connections_publish_only_their_own_role_scope(
    capability_env, wire, redis_client
):
    from app.core.credentials import encrypt_credentials

    env = capability_env
    with Session(engine) as session, session.begin():
        conn = TikTokConnection(
            tenant_id=env["context"].tenant_id,
            status="ACTIVE",
            credential_ciphertext=encrypt_credentials(
                tenant_id=env["context"].tenant_id,
                value={"access_token": "offline-other", "scope": "[611]"},
            ),
        )
        session.add(conn)
        session.flush()
        other_id = conn.id
        session.add(
            BCAccountAccess(
                tenant_id=env["context"].tenant_id,
                bc_id=env["bc_id"],
                advertiser_id="actual-account",
                connection_id=conn.id,
                in_bc=True,
                authorized=True,
                active=True,
            )
        )
    job = start(env)
    wire[1].append(page(["actual-account"]))
    run(env, redis_client, job)
    run(env, redis_client, job)
    with Session(engine) as session:
        other = session.get(
            BCAccountAccess,
            (env["context"].tenant_id, env["bc_id"], "actual-account", other_id),
        )
        assert other.permission_state == "UNKNOWN" and not other.can_build
    other_env = {**env, "connection_id": other_id, "request_id": uuid4()}
    other_job = start(other_env)
    assert job != other_job
    wire[1].append(page(["actual-account"], role="ANALYST"))
    run(other_env, redis_client, other_job)
    run(other_env, redis_client, other_job)
    with Session(engine) as session:
        for grant in session.exec(select(BCAccountAccess)):
            assert grant.can_build == (grant.connection_id == env["connection_id"])
            assert grant.can_upload == (grant.connection_id == env["connection_id"])
    assert len(wire[0]) == 2


def test_publish_page_rolls_back_all_grants_and_progress_on_failure(
    capability_env, wire, redis_client
):
    from sqlalchemy import event

    env = capability_env
    job_id = start(env)
    wire[1].append(page(["actual-account"]))
    run(env, redis_client, job_id)

    def fail_after_write(_conn, _cursor, statement, _params, _ctx, _many):
        if statement.startswith("UPDATE bc_account_access"):
            raise RuntimeError("offline sensitive transport-like detail")

    event.listen(engine, "after_cursor_execute", fail_after_write)
    try:
        job = run(env, redis_client, job_id)
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_write)
    assert job.status == "PENDING" and job.published_count == 0
    assert job.error_code == "capability_remote_unavailable"
    with Session(engine) as session:
        assert not session.exec(select(BCAccountAccess)).one().can_build


def test_start_and_status_preserve_tenant_bc_and_current_role(capability_env):
    from app.modules.accounts.capabilities import get_capability_job
    from app.modules.tenants.models import TenantMembership

    env = capability_env
    job_id = start(env)
    with Session(engine) as session:
        with pytest.raises(DomainError) as error:
            get_capability_job(
                session, context=env["context"], bc_id="another-bc", job_id=job_id
            )
        assert error.value.code == "capability_not_found"
    with Session(engine) as session, session.begin():
        session.exec(
            select(TenantMembership).where(
                TenantMembership.tenant_id == env["context"].tenant_id
            )
        ).one().role = "viewer"
    with pytest.raises(DomainError) as error:
        start({**env, "request_id": uuid4()})
    assert error.value.code == "action_forbidden"
    with Session(engine) as session:
        assert (
            get_capability_job(
                session, context=env["context"], bc_id=env["bc_id"], job_id=job_id
            ).id
            == job_id
        )


def test_account_capability_age_setting_has_finite_engineering_bounds():
    from pydantic import ValidationError

    from app.core.config import Settings, settings

    values = settings.model_dump()
    assert Settings.model_fields["BC_CAPABILITY_MAX_AGE_SECONDS"].default == 86400
    for invalid in [0, 59, 86401, 1.5, True]:
        with pytest.raises(ValidationError):
            Settings.model_validate(
                {**values, "BC_CAPABILITY_MAX_AGE_SECONDS": invalid}
            )
