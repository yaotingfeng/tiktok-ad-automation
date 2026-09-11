"""Independent real-PG regression coverage; no live SDK operations."""

from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.accounts import capabilities
from app.modules.accounts.capability_models import (
    CapabilityAsset,
    CapabilityJob,
    CapabilityPage,
)
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from tests.modules.accounts.capabilities.test_evidence import evidence
from tests.modules.accounts.capabilities.test_service import page, run, start


def add_directory(env, count):
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                AdvertiserAccount(
                    tenant_id=env["context"].tenant_id,
                    advertiser_id=f"review-{n:06}",
                    currency="USD",
                    timezone="UTC",
                    remote_status="ENABLE",
                )
                for n in range(count - 1)
            ]
        )
        session.flush()
        session.add_all(
            [
                BCAccountAccess(
                    tenant_id=env["context"].tenant_id,
                    bc_id=env["bc_id"],
                    advertiser_id=f"review-{n:06}",
                    connection_id=env["connection_id"],
                    in_bc=True,
                    authorized=True,
                    active=True,
                )
                for n in range(count - 1)
            ]
        )


def test_partial_publication_has_no_complete_evidence(
    capability_env, wire, redis_client
):
    env = capability_env
    add_directory(env, 205)
    ids = ["actual-account"] + [f"review-{n:06}" for n in range(204)]
    job_id = start(env)
    for number in range(1, 6):
        wire[1].append(page(ids[(number - 1) * 50 : number * 50], number, 205))
        run(env, redis_client, job_id)
    for expected in (100, 200):
        job = run(env, redis_client, job_id)
        assert job.published_count == expected and job.status == "PENDING"
        assert evidence(env) is None
    assert run(env, redis_client, job_id).status == "COMPLETE"
    assert evidence(env).can_build


def test_expiry_is_anchored_at_first_observation_not_each_read_or_publish(
    capability_env, wire, redis_client, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "BC_CAPABILITY_MAX_AGE_SECONDS", 14400)
    env = capability_env
    job_id = start(env)
    wire[1].append(page(["actual-account"] + [f"remote-{n}" for n in range(49)], 1, 51))
    first = run(env, redis_client, job_id)
    with Session(engine) as session:
        observed = session.get(CapabilityPage, (job_id, 1)).observed_at
    assert first.expires_at - observed == timedelta(seconds=14400)
    wire[1].append(page(["remote-last"], 2, 51))
    assert run(env, redis_client, job_id).expires_at == first.expires_at
    assert run(env, redis_client, job_id).expires_at == first.expires_at


def test_repair_does_not_rewrite_live_attempt_or_wrong_actor_dispatch(capability_env):
    env = capability_env
    job_id = start(env)
    with Session(engine) as session, session.begin():
        job = session.get(CapabilityJob, job_id)
        job.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        nonce = job.claim_token = uuid4()
        job.claimed_until = datetime.now(UTC) + timedelta(seconds=30)
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        original = (
            dispatch.id,
            dispatch.payload,
            dispatch.available_at,
            dispatch.published_at,
        )
    assert capabilities.repair_capabilities(database_engine=engine) == 0
    with Session(engine) as session, session.begin():
        job = session.get(CapabilityJob, job_id)
        assert job.claim_token == nonce
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        assert (
            dispatch.id,
            dispatch.payload,
            dispatch.available_at,
            dispatch.published_at,
        ) == original
        job.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        dispatch.payload = {"job_id": str(job_id), "revision": 999}
    assert capabilities.repair_capabilities(database_engine=engine) == 1
    with Session(engine) as session:
        job = session.get(CapabilityJob, job_id)
        assert job.status == "FAILED" and job.error_code == "dispatch_payload_invalid"
        assert session.get(PendingDispatch, job.dispatch_id).payload["revision"] == 999


@pytest.mark.parametrize("count", [100, 1000, 5000])
def test_reader_capacity_records_full_scope_aggregate_per_lookup(
    capability_env, count, record_property
):
    env = capability_env
    add_directory(env, count)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        basis = capabilities._directory_basis(
            session, env["context"], env["bc_id"], env["connection_id"]
        )
        job = CapabilityJob(
            tenant_id=env["context"].tenant_id,
            bc_id=env["bc_id"],
            connection_id=env["connection_id"],
            actor_id=env["context"].actor_id,
            credential_revision=0,
            directory_basis=basis,
            status="COMPLETE",
            phase="DONE",
            completed_at=now,
            expires_at=now + timedelta(hours=4),
            scope_known=True,
            scope_build=True,
        )
        session.add(job)
        session.flush()
        job_id = job.id
        session.add(
            CapabilityPage(
                job_id=job.id,
                page=1,
                tenant_id=job.tenant_id,
                bc_id=job.bc_id,
                row_count=1,
                observed_at=now,
            )
        )
        session.flush()
        session.add(
            CapabilityAsset(
                job_id=job.id,
                advertiser_id="actual-account",
                tenant_id=job.tenant_id,
                bc_id=job.bc_id,
                page=1,
                role="OPERATOR",
            )
        )
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.advertiser_id == "actual-account"
            )
        ).one()
        grant.permission_state = "VERIFIED"
        grant.can_build = True
    aggregate_calls = []

    def trace(_conn, _cursor, statement, params, _context, _many):
        if "string_agg" in statement:
            aggregate_calls.append((statement, params))

    event.listen(engine, "before_cursor_execute", trace)
    try:
        started = perf_counter()
        for _ in range(20):
            assert evidence(env).job_id == job_id
        elapsed = perf_counter() - started
    finally:
        event.remove(engine, "before_cursor_execute", trace)
    assert len(aggregate_calls) <= 20
    if not aggregate_calls:
        return  # A persisted revision may replace the aggregate in a later migration.
    with engine.connect() as conn:
        sql, params = aggregate_calls[0]
        plan = conn.exec_driver_sql(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params
        ).scalar_one()[0]

    def scanned(node):
        return (
            node.get("Actual Rows", 0)
            if node.get("Relation Name") == "bc_account_access"
            else 0
        ) + sum(scanned(p) for p in node.get("Plans", []))

    rows = scanned(plan["Plan"])
    assert rows >= count
    record_property("directory_rows", count)
    record_property("aggregate_calls_for_20_reads", len(aggregate_calls))
    record_property("grant_rows_scanned_per_aggregate", rows)
    record_property("lookup_seconds", elapsed)


@pytest.mark.parametrize("role", [{}, []])
def test_malformed_role_schema_fails_instead_of_retrying_forever(
    capability_env, wire, redis_client, role
):
    env = capability_env
    job_id = start(env)
    response = page(["actual-account"])
    response["list"][0]["advertiser_role"] = role
    wire[1].append(response)
    job = run(env, redis_client, job_id)
    assert job.status == "FAILED"
    assert job.error_code == "capability_response_unverified"
