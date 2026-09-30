"""B3 本地只读报表 API。

这些端点只访问本地发布表和查询快照；普通列表/趋势请求不会隐式调用 TikTok。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.reporting.detail import AdDetailPublic, get_ad_detail
from app.modules.reporting.queries import query_ads, snapshot_trend
from app.modules.reporting.schemas import (
    AdsQueryPage,
    FrozenSelection,
    ReportingFilter,
    SelectionRequest,
    TrendPublic,
)
from app.modules.reporting.selection import freeze_selection
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["ads_reporting"])
Dimension = Literal["account", "campaign", "adgroup", "ad", "material", "drama"]
CSV = Annotated[str | None, Query(max_length=4096)]


def _tuple(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _filters(
    *,
    dimension: Dimension,
    start_date: date,
    end_date: date,
    advertiser_id: str | None,
    ids: str | None,
    query: str | None,
    ad_types: str | None,
    operation_statuses: str | None,
    review_statuses: str | None,
    budget_modes: str | None,
    naming_status: str | None,
    min_spend: Decimal | None,
    max_spend: Decimal | None,
    min_d0_roas: Decimal | None,
    max_d0_roas: Decimal | None,
    min_target_roas: Decimal | None,
    max_target_roas: Decimal | None,
    created_from: date | None,
    created_to: date | None,
    sort_by: str,
    sort_direction: Literal["asc", "desc"],
) -> ReportingFilter:
    return ReportingFilter(
        dimension=dimension,
        start_date=start_date,
        end_date=end_date,
        advertiser_ids=(advertiser_id,) if advertiser_id else (),
        ids=_tuple(ids),
        query=query,
        ad_types=_tuple(ad_types),
        operation_statuses=_tuple(operation_statuses),
        review_statuses=_tuple(review_statuses),
        budget_modes=_tuple(budget_modes),
        naming_status=naming_status,
        min_spend=min_spend,
        max_spend=max_spend,
        min_d0_roas=min_d0_roas,
        max_d0_roas=max_d0_roas,
        min_target_roas=min_target_roas,
        max_target_roas=max_target_roas,
        created_from=created_from,
        created_to=created_to,
        sort_by=sort_by,
        sort_direction=sort_direction,
    )


def _context(session, user, tenant_id: UUID) -> TenantContext:
    return require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")


@router.get("/ads", response_model=AdsQueryPage, name="query_ads")
def query_ads_route(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    dimension: Dimension = "campaign",
    start_date: date = date.today(),
    end_date: date = date.today(),
    advertiser_id: str | None = Query(default=None, max_length=128),
    ids: CSV = None,
    query: CSV = None,
    ad_types: CSV = None,
    operation_statuses: CSV = None,
    review_statuses: CSV = None,
    budget_modes: CSV = None,
    naming_status: str | None = Query(default=None, max_length=32),
    min_spend: Decimal | None = None,
    max_spend: Decimal | None = None,
    min_d0_roas: Decimal | None = None,
    max_d0_roas: Decimal | None = None,
    min_target_roas: Decimal | None = None,
    max_target_roas: Decimal | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    sort_by: str = Query(default="row_key", max_length=32),
    sort_direction: Literal["asc", "desc"] = "asc",
    snapshot_id: UUID | None = None,
    cursor: Annotated[str | None, Query(max_length=8192)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AdsQueryPage:
    context = _context(session, user, tenant_id)
    filters = _filters(
        dimension=dimension,
        start_date=start_date,
        end_date=end_date,
        advertiser_id=advertiser_id,
        ids=ids,
        query=query,
        ad_types=ad_types,
        operation_statuses=operation_statuses,
        review_statuses=review_statuses,
        budget_modes=budget_modes,
        naming_status=naming_status,
        min_spend=min_spend,
        max_spend=max_spend,
        min_d0_roas=min_d0_roas,
        max_d0_roas=max_d0_roas,
        min_target_roas=min_target_roas,
        max_target_roas=max_target_roas,
        created_from=created_from,
        created_to=created_to,
        sort_by=sort_by,
        sort_direction=sort_direction,
    )
    result = query_ads(
        session,
        context=context,
        bc_id=bc_id,
        filters=filters,
        snapshot_id=snapshot_id,
        cursor=cursor,
        limit=limit,
    )
    # SessionDep rolls back on request teardown; persist the local snapshot so a
    # later cursor/trend/selection request can read the same frozen rows.
    session.commit()
    return result


@router.get("/ads/{kind}/{remote_id}", response_model=AdDetailPublic, name="ad_detail")
def ad_detail_route(
    tenant_id: UUID,
    kind: Literal["campaign", "adgroup", "ad", "creative"],
    remote_id: str,
    advertiser_id: Annotated[str, Query(min_length=1, max_length=128)],
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> AdDetailPublic:
    context = _context(session, user, tenant_id)
    return get_ad_detail(
        session,
        context=context,
        bc_id=bc_id,
        ref=EntityRef(tenant_id, advertiser_id, kind, remote_id),
    )


@router.get("/reports/trend", response_model=TrendPublic, name="report_trend")
def report_trend_route(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    dimension: Dimension = "campaign",
    start_date: date = date.today(),
    end_date: date = date.today(),
    advertiser_id: str | None = Query(default=None, max_length=128),
    ids: CSV = None,
    query: CSV = None,
    min_spend: Decimal | None = None,
    max_spend: Decimal | None = None,
    min_d0_roas: Decimal | None = None,
    max_d0_roas: Decimal | None = None,
    min_target_roas: Decimal | None = None,
    max_target_roas: Decimal | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    snapshot_id: UUID | None = None,
) -> TrendPublic:
    context = _context(session, user, tenant_id)
    filters = _filters(
        dimension=dimension,
        start_date=start_date,
        end_date=end_date,
        advertiser_id=advertiser_id,
        ids=ids,
        query=query,
        ad_types=None,
        operation_statuses=None,
        review_statuses=None,
        budget_modes=None,
        naming_status=None,
        min_spend=min_spend,
        max_spend=max_spend,
        min_d0_roas=min_d0_roas,
        max_d0_roas=max_d0_roas,
        min_target_roas=min_target_roas,
        max_target_roas=max_target_roas,
        created_from=created_from,
        created_to=created_to,
        sort_by="row_key",
        sort_direction="asc",
    )
    if snapshot_id is None:
        page = query_ads(session, context=context, bc_id=bc_id, filters=filters, limit=1)
        snapshot_id = page.snapshot.snapshot_id
        session.commit()
    return snapshot_trend(
        session,
        context=context,
        bc_id=bc_id,
        snapshot_id=snapshot_id,
        filters=filters,
    )


@router.post("/ad-selections", response_model=FrozenSelection, name="freeze_ad_selection")
def freeze_ad_selection_route(
    tenant_id: UUID,
    body: SelectionRequest,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> FrozenSelection:
    context = _context(session, user, tenant_id)
    result = freeze_selection(session, context=context, bc_id=bc_id, request=body)
    session.commit()
    return result
