from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.ads.models import AdObject


@pytest.fixture
def directory_seed(session, context, other_context):
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    bc = TenantBC(tenant_id=context.tenant_id, bc_id="bc-own")
    account = AdvertiserAccount(
        tenant_id=context.tenant_id,
        advertiser_id="account-a",
        currency="USD",
        timezone="UTC",
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
    )
    ref = EntityRef(context.tenant_id, account.advertiser_id, "ad", "90071992547409933")
    row = AdObject(
        tenant_id=ref.tenant_id,
        advertiser_id=ref.advertiser_id,
        kind=ref.kind,
        remote_id=ref.remote_id,
        parent_kind="adgroup",
        parent_remote_id="not-yet-seen",
        ad_type="SMART_PLUS",
        name="external ad",
        configuration={"budget": "1.234567"},
        operation_status="ENABLE",
        review_status="APPROVED",
        delivery_status="DELIVERING",
        observed_at=datetime.now(UTC),
        published_version=1,
        source_connection_id=connection.id,
        source_channel="OFFICIAL_API",
    )
    session.add_all([grant, row])
    session.flush()
    return SimpleNamespace(
        context=context,
        other_context=other_context,
        bc_id=bc.bc_id,
        ref=ref,
        row=row,
        grant=grant,
        connection=connection,
    )
