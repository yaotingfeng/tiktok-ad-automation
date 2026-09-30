from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import (
    AdEntity,
    CallEvidence,
    DirectoryPage,
    EntityRef,
)
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from app.modules.ads.models import AdObject
from app.modules.ads.sync import publish_directory, stage_directory_page
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting.sync_models import SyncSchedule


def _route(seed):
    return {
        "tenant_id": str(seed.context.tenant_id),
        "bc_id": seed.bc_id,
        "connection_id": str(seed.connection.id),
        "channel": seed.connection.kind,
        "authorization_revision": seed.connection.authorization_revision,
        "binding_revision": 0,
        "adapter_contract_revision": seed.connection.adapter_contract_revision,
    }


def _authority(session, seed):
    session.add(
        BCConnectionBinding(
            tenant_id=seed.context.tenant_id,
            bc_id=seed.bc_id,
            connection_id=seed.connection.id,
            kind=seed.connection.kind,
        )
    )
    session.add(
        ConnectionAuthorization(
            tenant_id=seed.context.tenant_id,
            connection_id=seed.connection.id,
            authorization_revision=seed.connection.authorization_revision,
            source="OFFLINE_TEST",
            verified_at=datetime.now(UTC),
            permission_summary={"read_authorized": True},
        )
    )
    seed.grant.checked_at = datetime.now(UTC)
    session.flush()


def _run(session, seed, *, kind="campaign", ad_type="REGULAR"):
    run = AdDirectoryRun(
        tenant_id=seed.context.tenant_id,
        advertiser_id=seed.ref.advertiser_id,
        bc_id=seed.bc_id,
        actor_id=seed.context.actor_id,
        connection_id=seed.connection.id,
        channel=seed.connection.kind,
        frozen_route=_route(seed),
        partition_key=(uuid4().hex * 2)[:64],
        query={"page_size": 100},
        claim_generation=1,
        kind=kind,
        ad_type=ad_type,
    )
    session.add(run)
    session.flush()
    return run


def _page(
    seed,
    *,
    page=1,
    complete=True,
    next_page=None,
    name="版权方-剧名-备注",
    ad_type="REGULAR",
):
    ref = EntityRef(
        seed.context.tenant_id, seed.ref.advertiser_id, "campaign", "campaign-1"
    )
    entity = AdEntity(
        ref=ref,
        parent_ref=None,
        ad_type=ad_type,
        name=name,
        configuration={"budget": "1.2"},
        operation_status="ENABLE",
        review_status="APPROVED",
        delivery_status="DELIVERING",
        observed_at=datetime.now(UTC),
    )
    return DirectoryPage(
        items=(entity,),
        materials=(),
        next_page=next_page,
        complete=complete,
        evidence=CallEvidence(request_id="test-request"),
        page=page,
    )


def test_staging_is_idempotent_and_conflicts_are_rejected(session, directory_seed):
    run = _run(session, directory_seed)
    page = _page(directory_seed)
    stage_directory_page(session, run_id=run.id, page=page, claim_generation=1)
    stage_directory_page(session, run_id=run.id, page=page, claim_generation=1)
    with pytest.raises(DomainError):
        stage_directory_page(
            session,
            run_id=run.id,
            page=_page(directory_seed, name="版权方-另一剧-备注"),
            claim_generation=1,
        )
    assert len(session.exec(select(AdDirectoryPage)).all()) == 1


def test_staging_rejects_pages_after_terminal_page(session, directory_seed):
    run = _run(session, directory_seed)
    stage_directory_page(
        session, run_id=run.id, page=_page(directory_seed), claim_generation=1
    )
    with pytest.raises(DomainError):
        stage_directory_page(
            session,
            run_id=run.id,
            page=_page(directory_seed, page=2),
            claim_generation=1,
        )


def test_staging_allows_earlier_page_after_out_of_order_terminal(
    session, directory_seed
):
    run = _run(session, directory_seed)
    stage_directory_page(
        session,
        run_id=run.id,
        page=_page(directory_seed, page=2),
        claim_generation=1,
    )
    stage_directory_page(
        session,
        run_id=run.id,
        page=_page(directory_seed, page=1, complete=False, next_page=2),
        claim_generation=1,
    )


def test_staging_rejects_wrong_automation_type(session, directory_seed):
    run = _run(session, directory_seed, kind="campaign")
    with pytest.raises(DomainError):
        stage_directory_page(
            session,
            run_id=run.id,
            page=_page(directory_seed, ad_type="SMART_PLUS"),
            claim_generation=1,
        )


def test_publish_requires_terminal_page_and_projects_name(session, directory_seed):
    _authority(session, directory_seed)
    run = _run(session, directory_seed)
    stage_directory_page(
        session,
        run_id=run.id,
        page=_page(directory_seed, complete=False, next_page=2),
        claim_generation=1,
    )
    with pytest.raises(DomainError):
        publish_directory(session, run_id=run.id, claim_generation=1)
    stage_directory_page(
        session, run_id=run.id, page=_page(directory_seed, page=2), claim_generation=1
    )
    version = publish_directory(session, run_id=run.id, claim_generation=1)
    assert version > 0
    campaign = session.get(
        AdObject,
        (directory_seed.context.tenant_id, "account-a", "campaign", "campaign-1"),
    )
    assert campaign is not None and campaign.published_version == version


def test_missing_parent_queues_targeted_follow_up(session, directory_seed):
    _authority(session, directory_seed)
    run = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    parent = EntityRef(
        directory_seed.context.tenant_id, "account-a", "adgroup", "missing-group"
    )
    entity = AdEntity(
        ref=directory_seed.ref,
        parent_ref=parent,
        ad_type="SMART_PLUS",
        name="ad",
        configuration={},
        operation_status="ENABLE",
        review_status=None,
        delivery_status=None,
        observed_at=datetime.now(UTC),
    )
    page = DirectoryPage(
        items=(entity,),
        materials=(),
        next_page=None,
        complete=True,
        evidence=CallEvidence(),
    )
    stage_directory_page(session, run_id=run.id, page=page, claim_generation=1)
    publish_directory(session, run_id=run.id, claim_generation=1)
    target = session.exec(
        select(SyncSchedule).where(SyncSchedule.scope == "targeted")
    ).one()
    assert target.requested_coverage["reason"] == "missing_parent"
    assert target.requested_coverage["directory_targets"][0]["ad_type"] == "SMART_PLUS"


def test_late_older_run_is_fenced_after_newer_run_publishes(session, directory_seed):
    _authority(session, directory_seed)
    older = _run(session, directory_seed)
    newer = _run(session, directory_seed)
    stage_directory_page(
        session, run_id=newer.id, page=_page(directory_seed), claim_generation=1
    )
    publish_directory(session, run_id=newer.id, claim_generation=1)
    stage_directory_page(
        session, run_id=older.id, page=_page(directory_seed), claim_generation=1
    )
    with pytest.raises(DomainError):
        publish_directory(session, run_id=older.id, claim_generation=1)
