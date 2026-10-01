from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.routing import freeze_route
from app.modules.reporting.query_models import FrozenSelectionRecord
from tests.modules.conftest import create_context


@pytest.fixture
def management_env(session):
    context = create_context(session)
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    bc = TenantBC(tenant_id=context.tenant_id, bc_id="management-bc")
    account = AdvertiserAccount(
        tenant_id=context.tenant_id,
        advertiser_id="management-account",
        currency="USD",
        timezone="UTC",
        remote_status="STATUS_ENABLE",
    )
    session.add_all([connection, bc, account])
    session.flush()
    grant = BCAccountAccess(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        advertiser_id=account.advertiser_id,
        connection_id=connection.id,
        in_bc=True,
        authorized=True,
        active=True,
        can_build=True,
        can_upload=True,
        permission_state="VERIFIED",
        checked_at=datetime.now(UTC),
    )
    session.add_all(
        [
            grant,
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                connection_id=connection.id,
                kind=connection.kind,
            ),
            ConnectionAuthorization(
                tenant_id=context.tenant_id,
                connection_id=connection.id,
                authorization_revision=connection.authorization_revision,
                scopes=["read", "build"],
                permission_summary={"read_authorized": True, "build_authorized": True},
                source="SYNTHETIC_COMPLETE_EVIDENCE",
                verified_at=datetime.now(UTC),
            ),
            BCDefaultRoute(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                connection_id=connection.id,
            ),
        ]
    )
    selection = FrozenSelectionRecord(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        actor_id=context.actor_id,
        snapshot_id=uuid4(),
        advertiser_ids=[account.advertiser_id],
        filters={},
        filter_digest="f" * 64,
        publication_versions={},
        naming_versions={},
        refs=[],
        material_uses=[],
        membership_digest="m" * 64,
    )
    session.add(selection)
    session.flush()
    return (
        context,
        bc,
        account,
        freeze_route(session, context=context, bc_id=bc.bc_id),
        selection,
    )
