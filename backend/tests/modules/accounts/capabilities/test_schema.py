"""Real PostgreSQL capability scope and evidence invariants."""

import importlib.util
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError


def test_capability_schema_enforces_tenant_scope_and_unique_remote_rows(
    session, context
):
    assert importlib.util.find_spec("app.modules.accounts.capability_models"), (
        "Capability persistence is missing"
    )
    from app.modules.accounts.capability_models import (
        CapabilityAsset,
        CapabilityJob,
        CapabilityPage,
    )
    from app.modules.accounts.models import TenantBC, TikTokConnection
    from app.modules.tenants.models import Tenant

    conn = TikTokConnection(tenant_id=context.tenant_id)
    foreign = Tenant(name="Other capability tenant")
    session.add_all(
        [conn, foreign, TenantBC(tenant_id=context.tenant_id, bc_id="test-bc")]
    )
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            CapabilityJob(
                tenant_id=foreign.id,
                bc_id="test-bc",
                connection_id=conn.id,
                actor_id=context.actor_id,
                credential_version=0,
                directory_basis="a" * 64,
            )
        )
        session.flush()
    job = CapabilityJob(
        tenant_id=context.tenant_id,
        bc_id="test-bc",
        connection_id=conn.id,
        actor_id=context.actor_id,
        credential_version=0,
        directory_basis="a" * 64,
    )
    session.add(job)
    session.flush()
    session.add(
        CapabilityPage(
            job_id=job.id,
            tenant_id=context.tenant_id,
            bc_id="test-bc",
            page=1,
            row_count=1,
        )
    )
    session.flush()
    session.add(
        CapabilityPage(
            job_id=job.id,
            tenant_id=context.tenant_id,
            bc_id="test-bc",
            page=2,
            row_count=1,
        )
    )
    session.flush()
    session.add(
        CapabilityAsset(
            tenant_id=context.tenant_id,
            bc_id="test-bc",
            job_id=job.id,
            advertiser_id="remote-only",
            page=1,
            role="ANALYST",
        )
    )
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            CapabilityAsset(
                tenant_id=context.tenant_id,
                bc_id="test-bc",
                job_id=job.id,
                advertiser_id="remote-only",
                page=2,
                role="OPERATOR",
            )
        )
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            CapabilityAsset(
                tenant_id=uuid4(),
                bc_id="test-bc",
                job_id=job.id,
                advertiser_id="foreign",
                page=1,
                role="OPERATOR",
            )
        )
        session.flush()
