from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts import capabilities
from app.modules.accounts.capability_models import CapabilityJob
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from tests.modules.accounts.capabilities.test_service import page, run, start


def evidence(env):
    with Session(engine) as session:
        return capabilities.get_capability_evidence(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            connection_id=env["connection_id"],
        )


def test_evidence_requires_complete_publication_and_reads_no_credentials(
    capability_env, wire, redis_client, monkeypatch
):
    assert hasattr(capabilities, "get_capability_evidence"), (
        "Pure capability evidence reader missing"
    )
    env = capability_env
    job_id = start(env)
    assert evidence(env) is None
    wire[1].append(page(["actual-account"]))
    run(env, redis_client, job_id)
    assert evidence(env) is None
    run(env, redis_client, job_id)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Pure read performed external work")

    for name in [
        "open_tiktok_gateway",
        "enqueue_after_commit",
    ]:
        monkeypatch.setattr(capabilities, name, forbidden)
    statements = []

    def record(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement.lstrip().split()[0])

    event.listen(engine, "before_cursor_execute", record)
    try:
        with Session(engine) as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            result = capabilities.get_capability_evidence(
                session,
                context=env["context"],
                bc_id=env["bc_id"],
                advertiser_id="actual-account",
                connection_id=env["connection_id"],
            )
            session.commit()
        assert (
            result.job_id == job_id
            and result.can_build
            and result.can_upload
            and result.scope_verified
        )
        assert result.evidence_ids == (job_id,) and result.page_number == 1
        assert set(statements) == {"SELECT", "SET"}
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(wire[0]) == 1


@pytest.mark.parametrize("change", ["expired", "version", "directory", "grant"])
def test_evidence_rejects_stale_or_revoked_authority(
    capability_env, wire, redis_client, change
):
    assert hasattr(capabilities, "get_capability_evidence")
    env = capability_env
    job_id = start(env)
    wire[1].append(page(["actual-account"]))
    run(env, redis_client, job_id)
    run(env, redis_client, job_id)
    with Session(engine) as session, session.begin():
        if change == "expired":
            session.get(CapabilityJob, job_id).expires_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
        elif change == "version":
            session.get(
                TikTokConnection, env["connection_id"]
            ).authorization_revision += 1
        elif change == "directory":
            session.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.tenant_id == env["context"].tenant_id
                )
            ).one().in_bc = False
        else:
            session.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.tenant_id == env["context"].tenant_id
                )
            ).one().can_build = False
    result = evidence(env)
    if change == "grant":
        assert result is not None and not result.can_build
    else:
        assert result is None


def test_long_running_pagination_does_not_renew_old_role_evidence(
    capability_env, wire, redis_client
):
    env = capability_env
    job_id = start(env)
    wire[1].append(page(["actual-account"] + [f"other-{n}" for n in range(49)], 1, 51))
    first = run(env, redis_client, job_id)
    assert first.expires_at is not None, "Freshness must start at first remote evidence"
    with Session(engine) as session, session.begin():
        session.get(CapabilityJob, job_id).expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    wire[1].append(page(["last"], 2, 51))
    job = run(env, redis_client, job_id)
    assert job.status == "STALE" and len(wire[0]) == 1


def test_normal_credential_refresh_keeps_frozen_authorization_evidence(
    capability_env, wire, redis_client
):
    env = capability_env
    job_id = start(env)
    wire[1].append(page(["actual-account"]))
    assert run(env, redis_client, job_id).phase == "PUBLISH"
    with Session(engine) as session, session.begin():
        session.get(TikTokConnection, env["connection_id"]).credential_revision += 1
    assert run(env, redis_client, job_id).status == "COMPLETE"
    assert evidence(env).job_id == job_id
    assert start({**env, "request_id": __import__("uuid").uuid4()}) == job_id


def test_observed_role_reduction_is_immediate_and_survives_later_page_failure(
    capability_env, wire, redis_client
):
    from app.core.credentials import encrypt_credentials
    from tests.modules.accounts.capabilities.conftest import seed_authority

    env = capability_env
    with Session(engine) as session, session.begin():
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == env["context"].tenant_id
            )
        ).one()
        grant.can_build = grant.can_upload = True
        grant.permission_state = "VERIFIED"
        other = TikTokConnection(
            tenant_id=grant.tenant_id,
            status="ACTIVE",
            credential_ciphertext=encrypt_credentials(
                tenant_id=grant.tenant_id, value={"access_token": "synthetic-other"}
            ),
        )
        session.add(other)
        session.flush()
        seed_authority(session, other, env["bc_id"])
        other_id = other.id
        session.add(
            BCAccountAccess(
                tenant_id=grant.tenant_id,
                bc_id=grant.bc_id,
                advertiser_id=grant.advertiser_id,
                connection_id=other_id,
                in_bc=True,
                authorized=True,
                active=True,
                can_build=True,
                can_upload=True,
                permission_state="VERIFIED",
            )
        )
    job_id = start(env)
    wire[1].append(
        page(
            ["actual-account"] + [f"other-{n}" for n in range(49)],
            1,
            51,
            role="ANALYST",
        )
    )
    assert run(env, redis_client, job_id).phase == "READ"
    with Session(engine) as session:
        own = session.get(
            BCAccountAccess,
            (
                env["context"].tenant_id,
                env["bc_id"],
                "actual-account",
                env["connection_id"],
            ),
        )
        other = session.get(
            BCAccountAccess,
            (env["context"].tenant_id, env["bc_id"], "actual-account", other_id),
        )
        assert not own.can_build and not own.can_upload
        assert own.active and own.authorized and own.in_bc
        assert other.can_build and other.can_upload

    def unavailable():
        raise RuntimeError("synthetic-private-error")

    wire[1].append(unavailable)
    assert run(env, redis_client, job_id).status == "PENDING"
    with Session(engine) as session:
        own = session.get(
            BCAccountAccess,
            (
                env["context"].tenant_id,
                env["bc_id"],
                "actual-account",
                env["connection_id"],
            ),
        )
        assert not own.can_build and not own.can_upload


def test_unknown_scope_immediately_blocks_old_flags_without_inventing_revocation(
    capability_env, wire, redis_client
):
    from app.modules.accounts.connection_models import ConnectionAuthorization

    env = capability_env
    with Session(engine) as session, session.begin():
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == env["context"].tenant_id
            )
        ).one()
        grant.can_build = grant.can_upload = True
        grant.permission_state = "VERIFIED"
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.tenant_id == env["context"].tenant_id
            )
        ).one()
        facts.permission_summary = {
            "read_authorized": True,
            "build_authorized": None,
            "upload_authorized": None,
        }
        facts.scopes = []
    job_id = start(env)
    wire[1].append(
        page(
            ["actual-account"] + [f"other-{n}" for n in range(49)], 1, 51, role="ADMIN"
        )
    )
    assert run(env, redis_client, job_id).phase == "READ"
    with Session(engine) as session:
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == env["context"].tenant_id
            )
        ).one()
        assert not grant.can_build and not grant.can_upload
        assert grant.permission_state == "UNKNOWN"
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.tenant_id == env["context"].tenant_id
            )
        ).one()
        assert facts.permission_summary["build_authorized"] is None
        assert facts.permission_summary["upload_authorized"] is None
