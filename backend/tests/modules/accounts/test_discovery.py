"""目录发布单元回归：输入已观察页，完整性失败不得触碰旧快照。"""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.credentials import encrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.discovery import AUTHORIZED_LIST_SOURCE
from app.integrations.tiktok.official.authorization import material_authorization
from app.modules.accounts.api_directory import SCHEMA_DIGEST
from app.modules.accounts.discovery import finalize_directory
from app.modules.accounts.discovery_models import DiscoveryStagedPage
from app.modules.accounts.mcp_discovery_tasks import stage_results
from app.modules.accounts.models import (
    AdvertiserAccount,
    AuthorizationAttempt,
    BCAccountAccess,
    DiscoveryRun,
    TenantBC,
    TikTokConnection,
)

ROW = {
    "advertiser_id": "90071992547409931",
    "name": "A",
    "currency": "USD",
    "timezone": "UTC",
    "remote_status": "ENABLE",
}


def staged(session, run, stage, rows, *, bc_id="", page=1, total_pages=1, last=True):
    run.work = {**run.work, "bc_id": bc_id}
    stage_results(
        session,
        run=run,
        schema_digest=SCHEMA_DIGEST,
        rows=[
            {
                "stage": stage,
                "page": page,
                "total_pages": total_pages,
                "total_number": None if stage == "DETAILS" else len(rows),
                "last_page": last,
                "pagination_kind": "FULL_RESPONSE"
                if stage in {"SUBJECT", "AUTHORIZED"}
                else "EXPLICIT_IDS"
                if stage == "DETAILS"
                else "REMOTE",
                "rows": rows,
                "call_evidence": {"completeness_source": AUTHORIZED_LIST_SOURCE}
                if stage == "AUTHORIZED"
                else {},
            }
        ],
    )


def seed_complete(session, run, bc_id, rows, *, role=None):
    facts = asdict(
        material_authorization({"scope": "[2,6]"}, observed_at=datetime.now(UTC))
    )
    facts["scopes"] = list(facts["scopes"])
    facts["observed_at"] = facts["observed_at"].isoformat()
    staged(session, run, "SUBJECT", [facts])
    staged(
        session,
        run,
        "AUTHORIZED",
        [{"advertiser_id": r["advertiser_id"]} for r in rows],
    )
    staged(session, run, "BCS", [{"bc_id": bc_id, "name": "BC"}])
    staged(
        session,
        run,
        "ASSETS",
        [
            {"advertiser_id": r["advertiser_id"], "name": r.get("name", "")}
            for r in rows
        ],
        bc_id=bc_id,
    )
    staged(
        session,
        run,
        "DETAILS",
        [
            {
                "name": "",
                "currency": "",
                "timezone": "",
                "remote_status": "UNKNOWN",
                **r,
            }
            for r in rows
        ],
        bc_id=bc_id,
    )
    staged(
        session,
        run,
        "ROLES",
        [{"advertiser_id": r["advertiser_id"], "role": role} for r in rows],
        bc_id=bc_id,
    )
    run.work = {"stage": "FINALIZE", "bc_id": bc_id, "page": 1}
    session.add(run)
    session.flush()


@pytest.fixture
def discovery_run(session, context):
    from app.modules.tenants.models import TenantMembership

    session.get(
        TenantMembership, (context.tenant_id, context.actor_id)
    ).role = "tenant_admin"
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(connection)
    session.flush()
    bc = TenantBC(tenant_id=context.tenant_id, bc_id="1234567890123456789")
    account = AdvertiserAccount(
        tenant_id=context.tenant_id,
        advertiser_id="old-account",
        currency="USD",
        timezone="UTC",
        remote_status="ENABLE",
    )
    session.add_all([bc, account])
    session.flush()
    old = BCAccountAccess(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        advertiser_id=account.advertiser_id,
        connection_id=connection.id,
        in_bc=True,
        authorized=True,
        active=True,
    )
    attempt = AuthorizationAttempt(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        state_hash=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        status="CANDIDATE_READY",
        candidate_ciphertext=encrypt_credentials(
            tenant_id=context.tenant_id,
            value={"access_token": "synthetic", "scope": "[2,6]"},
        ),
    )
    session.add(attempt)
    session.flush()
    run = DiscoveryRun(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        candidate_attempt_id=attempt.id,
        work={"stage": "SUBJECT", "page": 1},
    )
    session.add_all([old, run])
    session.flush()
    return run, old


def test_partial_run_does_not_remove_old_access(session, discovery_run):
    run, old = discovery_run
    staged(session, run, "ASSETS", [ROW], bc_id=old.bc_id)
    with pytest.raises(DomainError) as error:
        finalize_directory(session, run_id=run.id)
    assert error.value.code == "discovery_incomplete"
    session.refresh(old)
    assert old.active
    assert session.get(AdvertiserAccount, (run.tenant_id, ROW["advertiser_id"])) is None


def test_duplicate_page_rejected_without_partial_grant_publication(
    session, discovery_run
):
    run, old = discovery_run
    staged(session, run, "ASSETS", [ROW], bc_id=old.bc_id)
    with pytest.raises(DomainError):
        staged(session, run, "ASSETS", [ROW], bc_id=old.bc_id)
    assert (
        len(
            session.exec(
                select(DiscoveryStagedPage).where(DiscoveryStagedPage.run_id == run.id)
            ).all()
        )
        == 1
    )
    assert (
        session.get(
            BCAccountAccess,
            (run.tenant_id, old.bc_id, ROW["advertiser_id"], run.connection_id),
        )
        is None
    )


def test_finalization_requires_bc_end_evidence(session, discovery_run):
    run, old = discovery_run
    seed_complete(session, run, old.bc_id, [])
    session.delete(session.get(DiscoveryStagedPage, (run.id, "", "BCS", 1)))
    session.flush()
    with pytest.raises(DomainError) as error:
        finalize_directory(session, run_id=run.id)
    assert error.value.code == "discovery_incomplete"
    assert old.active


def test_complete_scan_retires_only_own_connection(session, discovery_run):
    run, old = discovery_run
    other = TikTokConnection(tenant_id=run.tenant_id, status="ACTIVE")
    session.add(other)
    session.flush()
    grant = BCAccountAccess(
        tenant_id=run.tenant_id,
        bc_id=old.bc_id,
        advertiser_id=old.advertiser_id,
        connection_id=other.id,
        in_bc=True,
        authorized=True,
        active=True,
    )
    session.add(grant)
    session.flush()
    seed_complete(session, run, old.bc_id, [ROW])
    finalize_directory(session, run_id=run.id)
    session.refresh(old)
    session.refresh(grant)
    assert run.status == "COMPLETE" and not old.active and grant.active
    finalize_directory(session, run_id=run.id)


def test_missing_metadata_retained_blocked(session, discovery_run):
    run, old = discovery_run
    seed_complete(session, run, old.bc_id, [{"advertiser_id": "missing"}])
    finalize_directory(session, run_id=run.id)
    grant = session.get(
        BCAccountAccess, (run.tenant_id, old.bc_id, "missing", run.connection_id)
    )
    assert grant.permission_state == "METADATA_INCOMPLETE"
    assert not grant.active and not grant.can_build and not grant.can_upload


def test_stale_generation_cannot_finalize(session, discovery_run):
    run, old = discovery_run
    seed_complete(session, run, old.bc_id, [ROW])
    session.get(TikTokConnection, run.connection_id).credential_revision += 1
    session.flush()
    with pytest.raises(DomainError) as error:
        finalize_directory(session, run_id=run.id)
    assert error.value.code == "discovery_stale"
    assert old.active


def test_page_gap_and_after_last_page_rejected(session, discovery_run):
    run, old = discovery_run
    with pytest.raises(DomainError):
        staged(session, run, "ASSETS", [], bc_id=old.bc_id, page=2, total_pages=2)
    staged(session, run, "ASSETS", [], bc_id=old.bc_id)
    with pytest.raises(DomainError):
        staged(session, run, "ASSETS", [], bc_id=old.bc_id, page=2, total_pages=2)


@pytest.mark.parametrize("account_role", ["ADMIN", "OPERATOR", "ANALYST", None])
def test_api_directory_publishes_actual_role_permissions_without_a_second_job(
    session, discovery_run, context, account_role
):
    from app.modules.accounts.capabilities import get_capability_evidence

    run, old = discovery_run
    seed_complete(session, run, old.bc_id, [ROW], role=account_role)
    finalize_directory(session, run_id=run.id)
    session.expire_all()
    grant = session.get(
        BCAccountAccess,
        (run.tenant_id, old.bc_id, ROW["advertiser_id"], run.connection_id),
    )
    expected = account_role in {"ADMIN", "OPERATOR"}
    assert grant.can_build == grant.can_upload == expected
    proof = get_capability_evidence(
        session,
        context=context,
        bc_id=old.bc_id,
        advertiser_id=ROW["advertiser_id"],
        connection_id=run.connection_id,
    )
    assert proof is not None
    assert proof.can_build == proof.can_upload == expected
