# backend/tests/modules/reporting/sync_conftest.py
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models import User
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.tenants.models import Tenant, TenantMembership


@pytest.fixture
def sync_seed(session):
    """真实 PostgreSQL：一个租户、一个 API 连接、两个 BC（bc-a、bc-b），
    账户 acc-1（香港时区）只在 bc-a，acc-2（UTC）同时在 bc-a 与 bc-b。"""
    actor, tenant = uuid4(), uuid4()
    session.add(
        User(
            id=actor,
            username=f"sync-{actor.hex[:10]}",
            hashed_password="x",
            is_active=True,
        )
    )
    session.add(Tenant(id=tenant, name=f"t-{tenant.hex[:8]}"))
    session.flush()
    session.add(
        TenantMembership(tenant_id=tenant, user_id=actor, role="operator", active=True)
    )
    connection = TikTokConnection(
        tenant_id=tenant,
        status="ACTIVE",
        kind="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
    )
    session.add_all(
        [
            TenantBC(tenant_id=tenant, bc_id="bc-a"),
            TenantBC(tenant_id=tenant, bc_id="bc-b"),
            connection,
        ]
    )
    session.flush()
    session.add_all(
        [
            AdvertiserAccount(
                tenant_id=tenant,
                advertiser_id="acc-1",
                currency="USD",
                timezone="Asia/Hong_Kong",
                name="账户一",
            ),
            AdvertiserAccount(
                tenant_id=tenant,
                advertiser_id="acc-2",
                currency="USD",
                timezone="UTC",
                name="账户二",
            ),
            ConnectionAuthorization(
                tenant_id=tenant,
                connection_id=connection.id,
                authorization_revision=0,
                source="OFFLINE_TEST",
                verified_at=datetime.now(UTC),
                permission_summary={"read_authorized": True},
            ),
        ]
    )
    session.flush()
    for bc in ("bc-a", "bc-b"):
        session.add(
            BCConnectionBinding(
                tenant_id=tenant,
                bc_id=bc,
                connection_id=connection.id,
                kind="OFFICIAL_API",
                status="ACTIVE",
                authorization_revision=0,
                revision=0,
            )
        )
    for bc, acc in (("bc-a", "acc-1"), ("bc-a", "acc-2"), ("bc-b", "acc-2")):
        session.add(
            BCAccountAccess(
                tenant_id=tenant,
                bc_id=bc,
                advertiser_id=acc,
                connection_id=connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=datetime.now(UTC),
            )
        )
    session.flush()
    return SimpleNamespace(
        tenant_id=tenant,
        actor_id=actor,
        connection=connection,
        context=SimpleNamespace(tenant_id=tenant, actor_id=actor, role="operator"),
    )


def ad_row(
    ad,
    day,
    *,
    campaign="c1",
    adgroup="g1",
    spend="1.00",
    d0="0.50",
    total="0.50",
    adv="acc-1",
):
    return {
        "dimensions": {"ad_id_v2": ad, "stat_time_day": f"{day} 00:00:00"},
        "metrics": {
            "advertiser_id": adv,
            "campaign_id": campaign,
            "adgroup_id": adgroup,
            "spend": spend,
            "impressions": "10",
            "clicks": "1",
            "native_growth_total_ad_impression_event_count": "1",
            "native_growth_ad_revenue_value_d0": d0,
            "native_growth_ad_revenue_value_d2": d0,
            "native_growth_ad_revenue_value_d6": d0,
            "native_growth_ad_revenue_value_d13": d0,
            "native_growth_ad_revenue_value_d29": d0,
            "native_growth_total_ad_impression_value": total,
            "campaign_name": f"嘉书-剧{campaign}-备注",
            "adgroup_name": "组",
            "ad_name": "广告",
            "campaign_automation_type": "UPGRADED_SMART_PLUS",
        },
    }
