"""A large directory does not turn each evidence read into an aggregate scan."""

from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import Session

from app.core.db import engine
from app.core.errors import DomainError
from app.modules.accounts import capabilities
from tests.modules.accounts.capabilities.test_service import page, run, start


def test_5000_accounts_complete_and_evidence_uses_only_point_revision_reads(
    capability_env, wire, redis_client, monkeypatch
):
    env = capability_env
    params = {
        "tenant": env["context"].tenant_id,
        "bc": env["bc_id"],
        "connection": env["connection_id"],
    }
    with Session(engine) as session, session.begin():
        session.execute(
            text(
                "INSERT INTO advertiser_account (tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict) SELECT :tenant,'scale-'||i,'','USD','UTC','ENABLE',false FROM generate_series(1,4999) i"
            ),
            params,
        )
        session.execute(
            text(
                "INSERT INTO bc_account_access (tenant_id,bc_id,connection_id,advertiser_id,in_bc,authorized,active,can_build,can_upload,permission_state) SELECT :tenant,:bc,:connection,'scale-'||i,true,true,true,false,false,'UNKNOWN' FROM generate_series(1,4999) i"
            ),
            params,
        )
    job_id = start(env)
    wire[1].append(page(["actual-account"]))
    for _ in range(53):
        job = run(env, redis_client, job_id)
        if job.status != "PENDING":
            break
    assert job.status == "COMPLETE"
    assert len(wire[0]) == 1

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Evidence reader attempted external work or a write")

    for name in [
        "open_tiktok_gateway",
        "enqueue_after_commit",
    ]:
        monkeypatch.setattr(capabilities, name, forbidden)
    statements = []

    def capture(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            for _ in range(20):
                evidence = capabilities.get_capability_evidence(
                    session,
                    context=env["context"],
                    bc_id=env["bc_id"],
                    advertiser_id="actual-account",
                    connection_id=env["connection_id"],
                )
                assert evidence is not None and evidence.can_build
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any(
        "string_agg" in sql or "jsonb_build_array" in sql for sql in statements
    )
    revisions = [sql for sql in statements if "account_directory_revision" in sql]
    assert len(revisions) == 20
    assert all(" join " not in sql and "where " in sql for sql in revisions)
    assert all(sql.lstrip().split()[0] in {"select", "set"} for sql in statements)


def test_missing_revision_fails_closed_without_lazy_creation(
    capability_env, wire, redis_client
):
    env = capability_env
    job_id = start(env)
    wire[1].append(page(["actual-account"]))
    run(env, redis_client, job_id)
    run(env, redis_client, job_id)
    with Session(engine) as session, session.begin():
        session.execute(
            text("DELETE FROM account_directory_revision WHERE tenant_id=:tenant"),
            {"tenant": env["context"].tenant_id},
        )
    with Session(engine) as session, session.begin():
        session.execute(text("SET TRANSACTION READ ONLY"))
        assert (
            capabilities.get_capability_evidence(
                session,
                context=env["context"],
                bc_id=env["bc_id"],
                advertiser_id="actual-account",
                connection_id=env["connection_id"],
            )
            is None
        )
    with pytest.raises(DomainError) as error:
        start({**env, "request_id": uuid4()})
    assert error.value.code == "capability_unavailable"
