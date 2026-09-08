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
        "decrypt_credentials",
        "sdk_client",
        "admitted_account_call",
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
            session.get(TikTokConnection, env["connection_id"]).credential_version += 1
        elif change == "directory":
            session.exec(select(BCAccountAccess)).one().in_bc = False
        else:
            session.exec(select(BCAccountAccess)).one().can_build = False
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
