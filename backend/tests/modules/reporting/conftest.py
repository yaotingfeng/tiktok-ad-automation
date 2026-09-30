from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.integrations.tiktok.contracts.ads import EntityRef
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
from app.modules.ads.models import AdObject
from app.modules.reporting.models import ReportFact, ReportObservation
from app.modules.reporting.sync_models import ReportSyncRun
from app.modules.tenants.models import Tenant, TenantMembership


@pytest.fixture
def observations(session, context):
    """RF4 的真实 PostgreSQL 观测记录：修正、备注改名和跨日各自可区分。"""
    session.add(
        AdvertiserAccount(
            tenant_id=context.tenant_id,
            advertiser_id="report-account",
            currency="USD",
            timezone="UTC",
        )
    )
    session.flush()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    common = {
        "tenant_id": context.tenant_id,
        "advertiser_id": "report-account",
        "granularity": "DAY",
        "report_contract": "basic_campaign",
        "metric_family": "delivery",
        "currency": "USD",
        "timezone": "UTC",
        "attribution": "default",
        "subject_kind": "campaign",
        "subject_key": ["campaign", "campaign-1"],
        "membership_digest": "a" * 64,
        "name_revision": 1,
        "grouping_revision": 1,
        "published_version": 1,
    }
    previous = ReportObservation(
        **common,
        bucket_start=start,
        bucket_end=start + timedelta(days=1),
        values={"spend": "10.00", "impressions": "100"},
        availability={"spend": "AVAILABLE", "impressions": "AVAILABLE"},
        observed_at=datetime(2026, 9, 1, 1, tzinfo=UTC),
    )
    corrected = ReportObservation(
        **common,
        bucket_start=start,
        bucket_end=start + timedelta(days=1),
        values={"spend": "8.75", "impressions": "100"},
        availability={"spend": "AVAILABLE", "impressions": "AVAILABLE"},
        observed_at=datetime(2026, 9, 1, 2, tzinfo=UTC),
    )
    renamed = ReportObservation(
        **(common | {"name_revision": 2}),
        bucket_start=start,
        bucket_end=start + timedelta(days=1),
        values={"spend": "8.75", "impressions": "100"},
        availability={"spend": "AVAILABLE", "impressions": "AVAILABLE"},
        observed_at=datetime(2026, 9, 1, 3, tzinfo=UTC),
    )
    next_day = ReportObservation(
        **common,
        bucket_start=start + timedelta(days=1),
        bucket_end=start + timedelta(days=2),
        values={"spend": "8.75", "impressions": "100"},
        availability={"spend": "AVAILABLE", "impressions": "AVAILABLE"},
        observed_at=datetime(2026, 9, 2, 1, tzinfo=UTC),
    )
    session.add_all([previous, corrected, renamed, next_day])
    session.flush()
    return SimpleNamespace(
        previous=previous,
        corrected=corrected,
        renamed=renamed,
        next_day=next_day,
    )


@pytest.fixture
def reporting_seed(session, context, observations):
    """提供报告运行所需的冻结 API 路由和账户授权事实。"""
    connection = TikTokConnection(
        tenant_id=context.tenant_id,
        status="ACTIVE",
        kind="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
    )
    session.add_all([TenantBC(tenant_id=context.tenant_id, bc_id="bc-report"), connection])
    session.flush()
    session.add_all(
        [
            BCAccountAccess(
                tenant_id=context.tenant_id,
                bc_id="bc-report",
                advertiser_id="report-account",
                connection_id=connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=datetime.now(UTC),
            ),
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id="bc-report",
                connection_id=connection.id,
                kind="OFFICIAL_API",
                status="ACTIVE",
                authorization_revision=0,
                revision=0,
            ),
            ConnectionAuthorization(
                tenant_id=context.tenant_id,
                connection_id=connection.id,
                authorization_revision=0,
                source="OFFLINE_TEST",
                verified_at=datetime.now(UTC),
                permission_summary={"read_authorized": True},
            ),
        ]
    )
    session.flush()
    return SimpleNamespace(context=context, connection=connection, observations=observations)


@pytest.fixture
def other_context() -> SimpleNamespace:
    """第二租户用于确认同名目录对象不能越过租户边界。"""
    return SimpleNamespace(tenant_id=uuid4(), actor_id=uuid4(), role="operator")


@pytest.fixture
def report_case(session, context, other_context):
    """B 查询场景：真实 PostgreSQL 中的租户、BC 授权、目录和事实。"""
    def seed_identity(scope):
        if session.get(User, scope.actor_id) is None:
            session.add(
                User(
                    id=scope.actor_id,
                    username=f"report-{scope.actor_id.hex[:12]}",
                    hashed_password="test-only",
                    is_active=True,
                )
            )
        if session.get(Tenant, scope.tenant_id) is None:
            session.add(Tenant(id=scope.tenant_id, name=f"report-{scope.tenant_id.hex[:12]}"))
        if session.get(TenantMembership, (scope.tenant_id, scope.actor_id)) is None:
            session.add(
                TenantMembership(
                    tenant_id=scope.tenant_id,
                    user_id=scope.actor_id,
                    role="operator",
                    active=True,
                )
            )
        session.flush()

    seed_identity(context)
    seed_identity(other_context)
    own_connection = TikTokConnection(
        tenant_id=context.tenant_id, status="ACTIVE", kind="OFFICIAL_API"
    )
    own_bc = TenantBC(tenant_id=context.tenant_id, bc_id="bc-report-case")
    session.add_all(
        [
            own_connection,
            own_bc,
            AdvertiserAccount(
                tenant_id=context.tenant_id,
                advertiser_id="report-account",
                currency="USD",
                timezone="UTC",
            ),
        ]
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=context.tenant_id,
            bc_id=own_bc.bc_id,
            advertiser_id="report-account",
            connection_id=own_connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            checked_at=datetime.now(UTC),
        )
    )
    other_connection = TikTokConnection(
        tenant_id=other_context.tenant_id, status="ACTIVE", kind="OFFICIAL_API"
    )
    other_bc = TenantBC(tenant_id=other_context.tenant_id, bc_id="bc-other")
    session.add_all(
        [
            other_connection,
            other_bc,
            AdvertiserAccount(
                tenant_id=other_context.tenant_id,
                advertiser_id="report-account",
                currency="USD",
                timezone="UTC",
            ),
        ]
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=other_context.tenant_id,
            bc_id=other_bc.bc_id,
            advertiser_id="report-account",
            connection_id=other_connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()

    def seed_campaign(
        name: str,
        advertiser_id: str,
        spend: Decimal | str | int,
        d0_revenue: Decimal | str | int,
        status: str = "ENABLE",
    ) -> EntityRef:
        account = AdvertiserAccount(
            tenant_id=context.tenant_id,
            advertiser_id=advertiser_id,
            currency="USD",
            timezone="UTC",
            remote_status=status,
        )
        if session.get(AdvertiserAccount, (context.tenant_id, advertiser_id)) is None:
            session.add(account)
            session.flush()
        ref = EntityRef(context.tenant_id, advertiser_id, "campaign", f"campaign-{uuid4().hex[:12]}")
        session.add(
            AdObject(
                tenant_id=ref.tenant_id,
                advertiser_id=ref.advertiser_id,
                kind=ref.kind,
                remote_id=ref.remote_id,
                ad_type="REGULAR",
                name=name,
                operation_status=status,
                review_status="APPROVED",
                observed_at=datetime.now(UTC),
                published_version=1,
                source_connection_id=own_connection.id,
                source_channel="OFFICIAL_API",
            )
        )
        session.flush()
        partition = sha256(ref.remote_id.encode()).hexdigest()
        route = {
            "tenant_id": str(context.tenant_id),
            "bc_id": own_bc.bc_id,
            "connection_id": str(own_connection.id),
            "channel": "OFFICIAL_API",
            "authorization_revision": 0,
            "adapter_contract_revision": own_connection.adapter_contract_revision,
            "binding_revision": 0,
        }
        run = ReportSyncRun(
            tenant_id=context.tenant_id,
            advertiser_id=advertiser_id,
            bc_id=own_bc.bc_id,
            actor_id=context.actor_id,
            connection_id=own_connection.id,
            channel="OFFICIAL_API",
            frozen_route=route,
            request_id=uuid4(),
            partition_key=partition,
            query={"report_contract": "basic_campaign"},
            status="COMPLETE",
            coverage="COMPLETE",
            next_page=1,
        )
        session.add(run)
        session.flush()
        start = datetime.combine(date(2026, 9, 30), time.min, tzinfo=UTC)
        end = start + timedelta(days=1)
        for metric_name, value in (
            ("spend", Decimal(str(spend))),
            ("native_growth_ad_revenue_value_d0", Decimal(str(d0_revenue))),
        ):
            session.add(
                ReportFact(
                    tenant_id=context.tenant_id,
                    advertiser_id=advertiser_id,
                    subject_key=["campaign", ref.remote_id],
                    bucket_start=start,
                    bucket_end=end,
                    granularity="DAY",
                    report_contract="basic_campaign",
                    metric_family="delivery",
                    currency="USD",
                    timezone="UTC",
                    attribution="default",
                    metric_name=metric_name,
                    value=value,
                    availability="AVAILABLE",
                    published_version=1,
                    request_sequence=1,
                    source_partition_key=partition,
                    source_run_id=run.id,
                )
            )
        session.flush()
        return ref

    return SimpleNamespace(
        session=session,
        context=context,
        other_context=other_context,
        bc_id=own_bc.bc_id,
        headers={"x-tenant-id": str(context.tenant_id), "x-actor-id": str(context.actor_id)},
        other_headers={
            "x-tenant-id": str(other_context.tenant_id),
            "x-actor-id": str(other_context.actor_id),
        },
        seed_campaign=seed_campaign,
        fact_date=date(2026, 9, 30),
        currency="USD",
        timezone="UTC",
    )
