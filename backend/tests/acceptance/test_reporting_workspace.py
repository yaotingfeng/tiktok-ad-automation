"""B6 阶段验收：工作台跨租户/BC 一致性与本地查询容量。

所有数据均在 PostgreSQL 测试库内合成；本文件不建立 TikTok/MCP 客户端，因而
可以把“本地读请求没有外部调用”作为明确断言。容量场景也只测聚合查询本身，
不把合成数据量写成真实业务规模。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlmodel import Session

from app.core.context import TenantContext
from app.core.db import engine
from app.integrations.tiktok.contracts.ads import EntityRef
from app.models import User
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.ads.models import AdMaterialReference, AdObject, CampaignNameProjection
from app.modules.reporting.aggregation import build_dimension_rows
from app.modules.reporting.exports import (
    create_export,
    download_export,
    generate_export,
)
from app.modules.reporting.models import ReportCoverage, ReportFact, ReportObservation
from app.modules.reporting.queries import query_ads, snapshot_trend
from app.modules.reporting.schemas import (
    AdsQueryPage,
    Dimension,
    ReportingFilter,
    SelectionRequest,
)
from app.modules.reporting.selection import freeze_selection
from app.modules.reporting.sync_models import ReportSyncRun
from app.modules.tenants.models import Tenant, TenantMembership


@dataclass(frozen=True)
class _Scope:
    context: TenantContext
    bc_id: str
    connection_id: UUID
    advertiser_id: str


@dataclass(frozen=True)
class _WorkspaceCase:
    primary: _Scope
    other: _Scope
    campaign_refs: tuple[EntityRef, ...]


def _scope(session: Session, *, label: str, bc_id: str) -> _Scope:
    tenant_id, actor_id = uuid4(), uuid4()
    session.add_all(
        [
            User(
                id=actor_id,
                username=f"b6-{label}-{actor_id.hex[:10]}",
                hashed_password="test-only",
                is_active=True,
            ),
            Tenant(id=tenant_id, name=f"B6 tenant {label}"),
            TenantMembership(
                tenant_id=tenant_id,
                user_id=actor_id,
                role="operator",
                active=True,
            ),
        ]
    )
    connection = TikTokConnection(
        tenant_id=tenant_id,
        status="ACTIVE",
        kind="OFFICIAL_API",
        adapter_contract_revision="official-api-v1",
    )
    session.add_all(
        [
            connection,
            TenantBC(tenant_id=tenant_id, bc_id=bc_id, name=f"B6 {bc_id}"),
            AdvertiserAccount(
                tenant_id=tenant_id,
                advertiser_id=f"account-{label}",
                name=f"Account {label}",
                currency="USD",
                timezone="UTC",
                remote_status="ENABLE",
            ),
        ]
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=tenant_id,
            bc_id=bc_id,
            advertiser_id=f"account-{label}",
            connection_id=connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            permission_state="VERIFIED",
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()
    return _Scope(
        context=TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator"),
        bc_id=bc_id,
        connection_id=connection.id,
        advertiser_id=f"account-{label}",
    )


def _run(
    session: Session,
    *,
    scope: _Scope,
    contract: str = "basic_campaign",
) -> ReportSyncRun:
    run = ReportSyncRun(
        tenant_id=scope.context.tenant_id,
        advertiser_id=scope.advertiser_id,
        bc_id=scope.bc_id,
        actor_id=scope.context.actor_id,
        connection_id=scope.connection_id,
        channel="OFFICIAL_API",
        frozen_route={
            "tenant_id": str(scope.context.tenant_id),
            "bc_id": scope.bc_id,
            "connection_id": str(scope.connection_id),
            "channel": "OFFICIAL_API",
            "authorization_revision": 0,
            "adapter_contract_revision": "official-api-v1",
            "binding_revision": 0,
        },
        request_id=uuid4(),
        partition_key=sha256(uuid4().bytes).hexdigest(),
        query={"report_contract": contract},
        status="COMPLETE",
        coverage="COMPLETE",
        published_version=1,
        observed_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        next_page=1,
    )
    session.add(run)
    session.flush()
    return run


def _campaign(
    session: Session,
    *,
    scope: _Scope,
    run: ReportSyncRun,
    index: int,
    provider: str,
    title: str,
    spend: Decimal,
    revenue: Decimal,
    status: str = "ENABLE",
) -> EntityRef:
    campaign_id = f"campaign-{index}"
    ref = EntityRef(scope.context.tenant_id, scope.advertiser_id, "campaign", campaign_id)
    observed_at = datetime(2026, 9, 30, 1, tzinfo=UTC)
    session.add(
        AdObject(
            tenant_id=scope.context.tenant_id,
            advertiser_id=scope.advertiser_id,
            kind="campaign",
            remote_id=campaign_id,
            ad_type="REGULAR",
            name=f"{provider}-{title}-素材{index}",
            operation_status=status,
            review_status="APPROVED",
            delivery_status="PAUSED" if status == "PAUSED" else "ACTIVE",
            observed_at=observed_at,
            published_version=1,
            source_connection_id=scope.connection_id,
            source_channel="OFFICIAL_API",
        )
    )
    session.add(
        CampaignNameProjection(
            tenant_id=scope.context.tenant_id,
            advertiser_id=scope.advertiser_id,
            campaign_remote_id=campaign_id,
            name_revision=1,
            raw_name=f"{provider}-{title}-素材{index}",
            provider_label=provider,
            drama_name=title,
            status="VALID",
            parser_revision=1,
            grouping_revision=1,
            observed_at=observed_at,
        )
    )
    start = datetime.combine(date(2026, 9, 30), dt_time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    partition = sha256(campaign_id.encode()).hexdigest()
    for metric_name, value in (
        ("spend", spend),
        ("native_growth_ad_revenue_value_d0", revenue),
    ):
        session.add(
            ReportFact(
                tenant_id=scope.context.tenant_id,
                advertiser_id=scope.advertiser_id,
                subject_key=["campaign", campaign_id],
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
    session.add(
        ReportCoverage(
            tenant_id=scope.context.tenant_id,
            advertiser_id=scope.advertiser_id,
            partition_key=partition,
            bucket_start=start,
            bucket_end=end,
            granularity="DAY",
            report_contract="basic_campaign",
            metric_family="delivery",
            currency="USD",
            timezone="UTC",
            attribution="default",
            filter_ids=[campaign_id],
            requested_metrics=["spend", "native_growth_ad_revenue_value_d0"],
            dimensions=["campaign"],
            status="COMPLETE",
            observed_at=observed_at,
            published_version=1,
            request_sequence=1,
            source_run_id=run.id,
        )
    )
    session.add(
        ReportObservation(
            tenant_id=scope.context.tenant_id,
            advertiser_id=scope.advertiser_id,
            bucket_start=start,
            bucket_end=end,
            granularity="DAY",
            report_contract="basic_campaign",
            metric_family="delivery",
            currency="USD",
            timezone="UTC",
            attribution="default",
            subject_kind="campaign",
            subject_key=["campaign", campaign_id],
            values={
                "spend": str(spend),
                "native_growth_ad_revenue_value_d0": str(revenue),
            },
            availability={
                "spend": "AVAILABLE",
                "native_growth_ad_revenue_value_d0": "AVAILABLE",
            },
            observed_at=observed_at,
            membership_digest="a" * 64,
            name_revision=1,
            grouping_revision=1,
            published_version=1,
        )
    )
    session.flush()
    return ref


def _external_material(session: Session, *, scope: _Scope, run: ReportSyncRun, ref: EntityRef) -> None:
    """外部素材没有本地 material_file，仍保留精确广告内使用身份。"""
    ad_id = "ad-external-1"
    session.add(
        AdObject(
            tenant_id=scope.context.tenant_id,
            advertiser_id=scope.advertiser_id,
            kind="ad",
            remote_id=ad_id,
            parent_kind="campaign",
            parent_remote_id=ref.remote_id,
            ad_type="REGULAR",
            name="外部素材广告",
            operation_status="ENABLE",
            review_status="APPROVED",
            observed_at=datetime.now(UTC),
            published_version=1,
            source_connection_id=scope.connection_id,
            source_channel="OFFICIAL_API",
        )
    )
    session.add(
        AdMaterialReference(
            tenant_id=scope.context.tenant_id,
            advertiser_id=scope.advertiser_id,
            ad_remote_id=ad_id,
            platform_material_id="vid-external-1",
            ad_material_id="ad-material-1",
            material_type="VIDEO",
            name="外部视频.mp4",
            complete=True,
            published_version=1,
        )
    )
    start = datetime.combine(date(2026, 9, 30), dt_time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    partition = sha256(b"external-material").hexdigest()
    for metric_name, value in (("spend", Decimal("7")),):
        session.add(
            ReportFact(
                tenant_id=scope.context.tenant_id,
                advertiser_id=scope.advertiser_id,
                subject_key=["material", "campaign", ref.remote_id, "vid-external-1", "VIDEO"],
                bucket_start=start,
                bucket_end=end,
                granularity="DAY",
                report_contract="material_overview",
                metric_family="delivery",
                currency="USD",
                timezone="UTC",
                attribution="default",
                metric_name=metric_name,
                value=value,
                availability="AVAILABLE",
                attributes={"ad_id": ad_id, "ad_material_id": "ad-material-1"},
                published_version=1,
                request_sequence=1,
                source_partition_key=partition,
                source_run_id=run.id,
            )
        )
    session.add(
        ReportCoverage(
            tenant_id=scope.context.tenant_id,
            advertiser_id=scope.advertiser_id,
            partition_key=partition,
            bucket_start=start,
            bucket_end=end,
            granularity="DAY",
            report_contract="material_overview",
            metric_family="delivery",
            currency="USD",
            timezone="UTC",
            attribution="default",
            filter_ids=["vid-external-1"],
            requested_metrics=["spend"],
            dimensions=["material"],
            status="COMPLETE",
            observed_at=datetime.now(UTC),
            published_version=1,
            request_sequence=1,
            source_run_id=run.id,
        )
    )
    session.flush()


@pytest.fixture
def workspace_case(session: Session) -> _WorkspaceCase:
    primary = _scope(session, label="primary", bc_id="bc-b6-primary")
    other = _scope(session, label="other", bc_id="bc-b6-other")
    primary_run = _run(session, scope=primary)
    other_run = _run(session, scope=other)
    providers = ("版权方甲", "版权方乙", "版权方丙")
    refs = tuple(
        _campaign(
            session,
            scope=primary,
            run=primary_run,
            index=index,
            provider=provider,
            title="同名剧",
            spend=Decimal(index * 10),
            revenue=Decimal(index * 20),
            status="PAUSED" if index == 2 else ("DELETED" if index == 3 else "ENABLE"),
        )
        for index, provider in enumerate(providers, start=1)
    )
    _external_material(session, scope=primary, run=primary_run, ref=refs[0])
    _campaign(
        session,
        scope=other,
        run=other_run,
        index=1,
        provider="其他版权方",
        title="同名剧",
        spend=Decimal("999"),
        revenue=Decimal("999"),
    )
    session.flush()
    return _WorkspaceCase(primary, other, refs)


def _filters(dimension: Dimension = "campaign") -> ReportingFilter:
    return ReportingFilter(
        dimension=dimension,
        start_date=date(2026, 9, 30),
        end_date=date(2026, 9, 30),
    )


def _summary_buckets(page: AdsQueryPage) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], page.summary["buckets"])


def _bucket_values(bucket: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], bucket["values"])


def test_workspace_fastapi_contract_has_no_external_write_routes() -> None:
    """B5 的本地 reporting 路由仍是唯一工作台合同，未新增平台写端点。"""
    from app.main import app

    paths = app.openapi()["paths"]
    expected = {
        "/api/tenants/{tenant_id}/ads",
        "/api/tenants/{tenant_id}/reports/trend",
        "/api/tenants/{tenant_id}/ad-selections",
        "/api/tenants/{tenant_id}/report-exports",
    }
    assert expected <= set(paths)
    reporting = [
        operation
        for path in paths.values()
        for operation in path.values()
        if operation.get("tags") == ["ads_reporting"]
    ]
    assert reporting
    assert all(operation["operationId"].startswith("ads_reporting-") for operation in reporting)


def test_workspace_http_query_contract(workspace_case: _WorkspaceCase, session: Session, client) -> None:
    """通过真实 FastAPI/TestClient 读取已发布本地报表，禁止隐式平台请求。"""
    from app.api.deps import get_current_user
    from app.main import app

    user = session.get(User, workspace_case.primary.context.actor_id)
    assert user is not None
    # B3 的生产路由提交快照；在这个 fixture 中把 commit 降为 flush，
    # 让根 conftest 的 PostgreSQL 外层事务仍能回滚合成数据。
    client_commit = session.commit
    session.commit = cast(Any, lambda: session.flush())
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = client.get(
            f"/api/tenants/{workspace_case.primary.context.tenant_id}/ads",
            params={
                "bc_id": workspace_case.primary.bc_id,
                "dimension": "campaign",
                "start_date": "2026-09-30",
                "end_date": "2026-09-30",
                "limit": 2,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["total"] == 3
        assert payload["snapshot"]["filters"]["dimension"] == "campaign"
    finally:
        session.commit = cast(Any, client_commit)
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


def test_workspace_snapshot_contract(workspace_case: _WorkspaceCase, session: Session) -> None:
    """列表、汇总、趋势、导出、全选共享同一租户/BC 冻结结果。"""
    case = workspace_case
    filters = _filters()
    page = query_ads(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        filters=filters,
        limit=2,
    )
    assert page.total == 3
    assert {row.display["operation_status"] for row in page.items} <= {"ENABLE", "PAUSED", "DELETED"}
    assert {row.display["provider"] for row in page.items} == {"版权方甲", "版权方乙", "版权方丙"}
    drama_page = query_ads(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        filters=_filters("drama"),
        limit=100,
    )
    assert drama_page.total == 3
    assert sum(Decimal(str(_bucket_values(bucket)["spend"])) for bucket in _summary_buckets(drama_page)) == Decimal("60")
    bucket = _summary_buckets(page)[0]
    values = _bucket_values(bucket)
    assert Decimal(str(values["spend"])) == Decimal("60")
    assert Decimal(str(values["native_growth_ad_revenue_value_d0"])) == Decimal("120")
    assert Decimal(str(values["d0_roas"])) == Decimal("2")

    trend = snapshot_trend(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        snapshot_id=page.snapshot.snapshot_id,
        filters=filters,
    )
    assert trend.coverage["status"] in {"COMPLETE", "INCOMPLETE"}
    assert trend.points
    assert sum(point.values["spend"] or Decimal(0) for point in trend.points) == Decimal("60")

    selection = freeze_selection(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        request=SelectionRequest(
            snapshot_id=page.snapshot.snapshot_id,
            mode="ALL_MATCHING",
        ),
    )
    assert {item.remote_id for item in selection.refs} == {ref.remote_id for ref in case.campaign_refs}

    export = create_export(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        snapshot_id=page.snapshot.snapshot_id,
        idempotency_key="b6-workspace-export",
    )
    # 直接调用本地 worker 函数，证明导出消费的是冻结副本且没有平台请求。
    from app.modules.reporting.query_models import ReportExport

    persisted = session.get(ReportExport, export.id)
    assert persisted is not None
    generate_export(session, persisted)
    session.flush()
    csv = download_export(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        export_id=export.id,
    ).decode()
    assert csv.splitlines()[0].startswith("row_key,")
    assert len(csv.splitlines()) == 4

    material_rows = build_dimension_rows(
        session,
        context=case.primary.context,
        bc_id=case.primary.bc_id,
        filters=_filters("material"),
    )
    assert len(material_rows) == 1
    assert material_rows[0].coverage["status"] == "COMPLETE"
    assert material_rows[0].material_uses[0].platform_material_id == "vid-external-1"

    other_page = query_ads(
        session,
        context=case.other.context,
        bc_id=case.other.bc_id,
        filters=filters,
        limit=100,
    )
    assert other_page.total == 1
    assert other_page.items[0].display["operation_status"] == "ENABLE"
    with pytest.raises(HTTPException):
        # 另一个租户的 BC 标识不能在当前租户下探测到数据或快照。
        query_ads(
            session,
            context=case.primary.context,
            bc_id=case.other.bc_id,
            filters=filters,
        )


def test_workspace_capacity_has_no_n_plus_one(workspace_case: _WorkspaceCase, session: Session) -> None:
    """合成 1,000 系列/10,000 广告只作为查询容量证据，不代表线上规模。"""
    case = workspace_case
    scope = case.primary
    now = datetime.now(UTC)
    campaigns: list[AdObject] = []
    ads: list[AdObject] = []
    for index in range(10_000):
        campaign_index = index // 10 + 10_000
        campaign_id = f"capacity-campaign-{campaign_index}"
        if index % 10 == 0:
            campaigns.append(
                AdObject(
                    tenant_id=scope.context.tenant_id,
                    advertiser_id=scope.advertiser_id,
                    kind="campaign",
                    remote_id=campaign_id,
                    ad_type="REGULAR",
                    name=f"capacity-series-{campaign_index}",
                    operation_status="ENABLE",
                    review_status="APPROVED",
                    observed_at=now,
                    published_version=1,
                    source_connection_id=scope.connection_id,
                    source_channel="OFFICIAL_API",
                )
            )
        ads.append(
            AdObject(
                tenant_id=scope.context.tenant_id,
                advertiser_id=scope.advertiser_id,
                kind="ad",
                remote_id=f"capacity-ad-{index}",
                parent_kind="campaign",
                parent_remote_id=campaign_id,
                ad_type="REGULAR",
                name=f"capacity-ad-{index}",
                operation_status="ENABLE",
                review_status="APPROVED",
                observed_at=now,
                published_version=1,
                source_connection_id=scope.connection_id,
                source_channel="OFFICIAL_API",
            )
        )
    session.add_all([*campaigns, *ads])
    session.flush()

    statements = 0

    def count_sql(*_args: object) -> None:
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_sql)
    started = time.perf_counter()
    try:
        rows = build_dimension_rows(
            session,
            context=scope.context,
            bc_id=scope.bc_id,
            filters=_filters("campaign"),
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert len(rows) >= 1_000
    # A fixed number of set-based reads is expected. A query per series/ad would
    # scale with input rows and is an N+1 regression even if the result is correct.
    assert statements <= 30, f"{statements} SQL statements for 1,000 series/10,000 ads"
    assert elapsed_ms < 30_000, f"synthetic capacity query exceeded 30s: {elapsed_ms:.1f}ms"
