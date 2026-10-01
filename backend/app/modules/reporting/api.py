"""B3 本地只读报表 API。

这些端点只访问本地发布表和查询快照；普通列表/趋势请求不会隐式调用 TikTok。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from sqlmodel import col, select

from app.api.deps import CurrentUser, SessionDep
from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.accounts.routing import freeze_route
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.detail import AdDetailPublic, get_ad_detail
from app.modules.reporting.exports import (
    create_export,
    download_export,
    get_export,
    list_exports,
)
from app.modules.reporting.preferences import (
    delete_view,
    list_views,
    save_view,
    update_view,
)
from app.modules.reporting.queries import query_ads, snapshot_trend
from app.modules.reporting.scheduling import SyncRequest, request_sync
from app.modules.reporting.schemas import (
    AdsQueryPage,
    ExportCreate,
    ExportPublic,
    FrozenSelection,
    ReportingFilter,
    SavedViewCreate,
    SavedViewPatch,
    SavedViewPublic,
    SelectionRequest,
    SyncRunPublic,
    SyncRunRequest,
    TrendPublic,
)
from app.modules.reporting.selection import freeze_selection
from app.modules.reporting.sync_models import ReportSyncRun
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["ads_reporting"])
Dimension = Literal["account", "campaign", "adgroup", "ad", "material", "drama"]
CSV = Annotated[str | None, Query(max_length=4096)]


def _sync_public(row: ReportSyncRun | AdDirectoryRun) -> SyncRunPublic:
    return SyncRunPublic(
        id=row.id,
        request_id=row.request_id,
        status=cast(Literal["QUEUED", "RUNNING", "COMPLETE", "FAILED", "EXPIRED"], row.status),
        coverage=row.coverage,
        error_code=row.error_code,
        observed_at=row.observed_at,
        completed_at=row.completed_at,
    )


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
    snapshot_id: UUID | None = None,
) -> AdDetailPublic:
    context = _context(session, user, tenant_id)
    return get_ad_detail(
        session,
        context=context,
        bc_id=bc_id,
        ref=EntityRef(tenant_id, advertiser_id, kind, remote_id),
        snapshot_id=snapshot_id,
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


@router.post("/report-views", response_model=SavedViewPublic, name="create_report_view")
def create_report_view_route(
    tenant_id: UUID,
    body: SavedViewCreate,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> SavedViewPublic:
    context = _context(session, user, tenant_id)
    result = save_view(
        session,
        context=context,
        bc_id=bc_id,
        name=body.name,
        filters=body.filters,
        columns=body.columns,
    )
    session.commit()
    return result


@router.get("/report-views", response_model=tuple[SavedViewPublic, ...], name="list_report_views")
def list_report_views_route(
    tenant_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> tuple[SavedViewPublic, ...]:
    context = _context(session, user, tenant_id)
    return list_views(session, context=context, bc_id=bc_id)


@router.patch(
    "/report-views/{view_id}",
    response_model=SavedViewPublic,
    name="update_report_view",
)
def update_report_view_route(
    tenant_id: UUID,
    view_id: UUID,
    body: SavedViewPatch,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> SavedViewPublic:
    context = _context(session, user, tenant_id)
    result = update_view(
        session,
        context=context,
        bc_id=bc_id,
        view_id=view_id,
        name=body.name,
        filters=body.filters,
        columns=body.columns,
    )
    session.commit()
    return result


@router.delete("/report-views/{view_id}", name="delete_report_view")
def delete_report_view_route(
    tenant_id: UUID,
    view_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> dict[str, bool]:
    context = _context(session, user, tenant_id)
    delete_view(session, context=context, bc_id=bc_id, view_id=view_id)
    session.commit()
    return {"deleted": True}


@router.post("/report-exports", response_model=ExportPublic, name="create_report_export")
def create_report_export_route(
    tenant_id: UUID,
    body: ExportCreate,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> ExportPublic:
    context = _context(session, user, tenant_id)
    result = create_export(
        session,
        context=context,
        bc_id=bc_id,
        snapshot_id=body.snapshot_id,
        idempotency_key=body.idempotency_key,
    )
    session.commit()
    return result


@router.get("/report-exports", response_model=tuple[ExportPublic, ...], name="list_report_exports")
def list_report_exports_route(
    tenant_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> tuple[ExportPublic, ...]:
    context = _context(session, user, tenant_id)
    return list_exports(session, context=context, bc_id=bc_id)


@router.get("/report-exports/{export_id}", response_model=ExportPublic, name="get_report_export")
def get_report_export_route(
    tenant_id: UUID,
    export_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> ExportPublic:
    context = _context(session, user, tenant_id)
    row = get_export(session, context=context, bc_id=bc_id, export_id=export_id)
    return ExportPublic(
        id=row.id,
        status=cast(Literal["QUEUED", "RUNNING", "COMPLETE", "FAILED", "EXPIRED"], row.status),
        coverage=dict(row.coverage),
        expires_at=row.expires_at,
    )


@router.get("/report-exports/{export_id}/download", name="download_report_export")
def download_report_export_route(
    tenant_id: UUID,
    export_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> Response:
    context = _context(session, user, tenant_id)
    content = download_export(session, context=context, bc_id=bc_id, export_id=export_id)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="report-{export_id}.csv"'},
    )


@router.post("/ad-sync-runs", response_model=SyncRunPublic, name="request_ad_sync")
def request_ad_sync_route(
    tenant_id: UUID,
    body: SyncRunRequest,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> SyncRunPublic:
    context = _context(session, user, tenant_id)
    # 先冻结当前默认通道，再由 request_sync 对每个账户重新核对当前授权。
    route = freeze_route(session, context=context, bc_id=bc_id, connection_id=None)
    request = SyncRequest(
        route=route,
        advertiser_ids=body.advertiser_ids,
        scope=body.scope,
        start_date=body.start_date,
        end_date=body.end_date,
        refs=tuple(
            EntityRef(ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id)
            for ref in body.refs
        ),
    )
    run_id = request_sync(session, context=context, request=request)
    session.commit()
    row = session.get(ReportSyncRun, run_id) or session.get(AdDirectoryRun, run_id)
    if row is None:
        raise RuntimeError("sync run was not persisted")
    return _sync_public(row)


@router.get("/ad-sync-runs", response_model=tuple[SyncRunPublic, ...], name="list_ad_sync_runs")
def list_ad_sync_runs_route(
    tenant_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> tuple[SyncRunPublic, ...]:
    context = _context(session, user, tenant_id)
    # 通过当前 BC 的授权集筛除撤权账户；轮询完全读取本地运行表。
    from app.modules.reporting.filters import authorized_grants

    allowed = {grant.advertiser_id for grant in authorized_grants(session, context=context, bc_id=bc_id)}
    reports = session.exec(
        select(ReportSyncRun)
        .where(
            ReportSyncRun.tenant_id == tenant_id,
            ReportSyncRun.bc_id == bc_id,
            ReportSyncRun.actor_id == context.actor_id,
            col(ReportSyncRun.advertiser_id).in_(allowed),
        )
        .order_by(col(ReportSyncRun.created_at).desc())
    ).all()
    directories = session.exec(
        select(AdDirectoryRun)
        .where(
            AdDirectoryRun.tenant_id == tenant_id,
            AdDirectoryRun.bc_id == bc_id,
            AdDirectoryRun.actor_id == context.actor_id,
            col(AdDirectoryRun.advertiser_id).in_(allowed),
        )
        .order_by(col(AdDirectoryRun.created_at).desc())
    ).all()
    return tuple(_sync_public(row) for row in [*reports, *directories])


@router.get("/ad-sync-runs/{run_id}", response_model=SyncRunPublic, name="get_ad_sync_run")
def get_ad_sync_run_route(
    tenant_id: UUID,
    run_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    session: SessionDep,
    user: CurrentUser,
) -> SyncRunPublic:
    context = _context(session, user, tenant_id)
    row = session.get(ReportSyncRun, run_id, populate_existing=True)
    if row is None:
        row = session.get(AdDirectoryRun, run_id, populate_existing=True)
    if row is None or row.tenant_id != tenant_id or row.bc_id != bc_id or row.actor_id != context.actor_id:
        raise HTTPException(404, detail="ad_sync_run_not_found")
    from app.modules.reporting.filters import authorized_grants

    allowed = {grant.advertiser_id for grant in authorized_grants(session, context=context, bc_id=bc_id)}
    if row.advertiser_id not in allowed:
        raise HTTPException(403, detail="report_scope_forbidden")
    return _sync_public(row)
