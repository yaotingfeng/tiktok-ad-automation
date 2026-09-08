import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.accounts.discovery import finalize_directory, save_directory_page
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    DiscoveryRun,
    DiscoverySeen,
    TenantBC,
    TikTokConnection,
)


@pytest.fixture
def discovery_run(session, context):
    from app.modules.tenants.models import TenantMembership

    session.get(
        TenantMembership, (context.tenant_id, context.actor_id)
    ).role = "tenant_admin"
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(connection)
    session.flush()
    bc = TenantBC(tenant_id=context.tenant_id, bc_id="1234567890123456789", name="BC")
    account = AdvertiserAccount(
        tenant_id=context.tenant_id,
        advertiser_id="old-account",
        name="old",
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
    run = DiscoveryRun(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        credential_version=0,
        status="RUNNING",
    )
    session.add_all([old, run])
    session.flush()
    return run, old


ROW = {
    "advertiser_id": "90071992547409931",
    "name": "A",
    "currency": "USD",
    "timezone": "UTC",
    "remote_status": "ENABLE",
}


def test_partial_run_does_not_remove_old_access(session, discovery_run):
    run, old = discovery_run
    save_directory_page(
        session,
        run_id=run.id,
        bc_id=old.bc_id,
        page=1,
        rows=[ROW],
        authorized_ids={ROW["advertiser_id"]},
        last_page=False,
    )
    with pytest.raises(DomainError) as error:
        finalize_directory(session, run_id=run.id)
    assert error.value.code == "discovery_incomplete"
    session.refresh(old)
    assert old.active


def test_duplicate_page_is_idempotent_and_unknown_is_blocked(session, discovery_run):
    run, old = discovery_run
    for _ in range(2):
        save_directory_page(
            session,
            run_id=run.id,
            bc_id=old.bc_id,
            page=1,
            rows=[ROW],
            authorized_ids={ROW["advertiser_id"]},
            last_page=True,
        )
    assert (
        len(
            session.exec(
                select(DiscoverySeen).where(DiscoverySeen.run_id == run.id)
            ).all()
        )
        == 1
    )
    access = session.get(
        BCAccountAccess,
        (run.tenant_id, old.bc_id, ROW["advertiser_id"], run.connection_id),
    )
    assert access.permission_state == "UNKNOWN"
    assert not access.can_build and not access.can_upload


def test_finalization_requires_bc_end_evidence(session, discovery_run):
    run, old = discovery_run
    save_directory_page(
        session,
        run_id=run.id,
        bc_id=old.bc_id,
        page=1,
        rows=[],
        authorized_ids=set(),
        last_page=True,
    )
    with pytest.raises(DomainError) as error:
        finalize_directory(session, run_id=run.id)
    assert error.value.code == "discovery_incomplete"


def finish_evidence(session, run, bc_id):
    run.work = {**run.work, "stage": "FINALIZE", "bc_ids": [bc_id]}
    session.add(
        DiscoverySeen(
            run_id=run.id, bc_id="", page=1, last_page=True, processed_count=1
        )
    )
    session.flush()


def test_complete_scan_retires_only_own_connection(session, discovery_run):
    run, old = discovery_run
    second = TikTokConnection(tenant_id=run.tenant_id, status="ACTIVE")
    session.add(second)
    session.flush()
    other = BCAccountAccess(
        tenant_id=run.tenant_id,
        bc_id=old.bc_id,
        advertiser_id=old.advertiser_id,
        connection_id=second.id,
        active=True,
        in_bc=True,
        authorized=True,
    )
    session.add(other)
    save_directory_page(
        session,
        run_id=run.id,
        bc_id=old.bc_id,
        page=1,
        rows=[ROW],
        authorized_ids={ROW["advertiser_id"]},
        last_page=True,
    )
    finish_evidence(session, run, old.bc_id)
    finalize_directory(session, run_id=run.id)
    session.refresh(old)
    session.refresh(other)
    assert run.status == "COMPLETE" and not old.active and other.active
    finalize_directory(session, run_id=run.id)


def test_missing_metadata_retained_blocked(session, discovery_run):
    run, old = discovery_run
    save_directory_page(
        session,
        run_id=run.id,
        bc_id=old.bc_id,
        page=1,
        rows=[{"advertiser_id": "missing"}],
        authorized_ids={"missing"},
        last_page=True,
    )
    access = session.get(
        BCAccountAccess, (run.tenant_id, old.bc_id, "missing", run.connection_id)
    )
    assert access.permission_state == "METADATA_INCOMPLETE"
    assert not access.active and not access.can_upload and not access.can_build


def test_stale_generation_cannot_write_or_finalize(session, discovery_run):
    run, old = discovery_run
    connection = session.get(TikTokConnection, run.connection_id)
    connection.credential_version += 1
    session.flush()
    for operation in (
        lambda: save_directory_page(
            session,
            run_id=run.id,
            bc_id=old.bc_id,
            page=1,
            rows=[ROW],
            authorized_ids=set(),
            last_page=True,
        ),
        lambda: finalize_directory(session, run_id=run.id),
    ):
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.code == "discovery_stale"
    assert old.active


def test_page_gap_and_after_last_page_rejected(session, discovery_run):
    run, old = discovery_run
    with pytest.raises(DomainError):
        save_directory_page(
            session,
            run_id=run.id,
            bc_id=old.bc_id,
            page=2,
            rows=[],
            authorized_ids=set(),
            last_page=True,
        )
    save_directory_page(
        session,
        run_id=run.id,
        bc_id=old.bc_id,
        page=1,
        rows=[],
        authorized_ids=set(),
        last_page=True,
    )
    with pytest.raises(DomainError):
        save_directory_page(
            session,
            run_id=run.id,
            bc_id=old.bc_id,
            page=2,
            rows=[],
            authorized_ids=set(),
            last_page=True,
        )
