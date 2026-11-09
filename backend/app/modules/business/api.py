"""经营API：请求只查投影或受理后台任务，不在页面请求中重算历史。"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_
from sqlmodel import col, select

from app.api.deps import CurrentUser, SessionDep
from app.core.context import TenantContext
from app.models import User
from app.modules.accounts.models import AdvertiserAccount
from app.modules.business.configuration import (
    bump_scopes,
    change_rate,
    read_rate_schedule,
    save_mapping,
    scope_accounts,
)
from app.modules.business.exports import check_export, create_export, export_chunks
from app.modules.business.models import (
    BusinessEvidence,
    BusinessMember,
    BusinessRate,
    BusinessRateSchedule,
    BusinessRun,
    BusinessScope,
)
from app.modules.business.projection import request_rebuild
from app.modules.business.queries import query_dashboard
from app.modules.business.rate_periods import RatePeriod
from app.modules.business.schemas import (
    DashboardFilter,
    DashboardPublic,
    EvidenceInput,
    EvidencePublic,
    ExportPublic,
    MappingInput,
    MemberInput,
    RateHistoryItem,
    RateHistoryPublic,
    RateInput,
    RatePreviewPublic,
    RatePublic,
    RateScope,
    RebuildInput,
)
from app.modules.providers.models import ProviderConnection, ProviderDrama
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}/business", tags=["business"])


@router.post("/query", response_model=DashboardPublic)
def dashboard(
    tenant_id: UUID, body: DashboardFilter, session: SessionDep, user: CurrentUser
) -> DashboardPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    return query_dashboard(session, context=context, filters=body)


@router.post("/rebuild", status_code=202)
def rebuild(
    tenant_id: UUID, body: RebuildInput, session: SessionDep, user: CurrentUser
) -> dict[str, list[str]]:
    # 经营数据与重算均仅管理员可用，仍保留BC和账户授权边界。
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    runs = request_rebuild(session, context=context, **body.model_dump())
    session.commit()
    return {"run_ids": [str(r.id) for r in runs]}


@router.get("/options")
def options(
    tenant_id: UUID, bc_id: str, session: SessionDep, user: CurrentUser
) -> dict[str, Any]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    allowed = scope_accounts(session, context, bc_id)
    accounts = (
        session.exec(
            select(AdvertiserAccount).where(
                col(AdvertiserAccount.tenant_id) == tenant_id,
                col(AdvertiserAccount.advertiser_id).in_(allowed),
            )
        ).all()
        if allowed
        else []
    )
    providers = session.exec(
        select(ProviderConnection)
        .where(
            col(ProviderConnection.tenant_id) == tenant_id,
            col(ProviderConnection.kind) != "other",
        )
        .order_by(col(ProviderConnection.display_name))
    ).all()
    cutoff = min(
        (
            datetime.now(UTC).astimezone(ZoneInfo(a.timezone)).date()
            - timedelta(days=1)
            for a in accounts
        ),
        default=datetime.now(UTC).date() - timedelta(days=1),
    )
    return {
        "accounts": [
            {
                "id": a.advertiser_id,
                "name": a.name or a.advertiser_id,
                "currency": a.currency,
                "timezone": a.timezone,
                "end_date": (
                    datetime.now(UTC).astimezone(ZoneInfo(a.timezone)).date()
                    - timedelta(days=1)
                ).isoformat(),
            }
            for a in accounts
        ],
        "providers": [{"id": str(p.id), "name": p.display_name} for p in providers],
        "end_date": cutoff.isoformat(),
    }


@router.get("/dramas")
def dramas(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    q: str = Query(default="", max_length=255),
    provider_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, Any]]:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="manage")
    statement = select(ProviderDrama).where(col(ProviderDrama.tenant_id) == tenant_id)
    if provider_id:
        statement = statement.where(col(ProviderDrama.connection_id) == provider_id)
    if q:
        statement = statement.where(
            or_(
                col(ProviderDrama.title).icontains(q, autoescape=True),
                col(ProviderDrama.external_drama_id).icontains(q, autoescape=True),
            )
        )
    return [
        {
            "id": str(d.id),
            "title": d.title,
            "external_id": d.external_drama_id,
            "provider_id": str(d.connection_id),
            "application_id": d.application_id,
        }
        for d in session.exec(
            statement.order_by(col(ProviderDrama.title), col(ProviderDrama.id)).limit(
                limit
            )
        ).all()
    ]


def _rate_context(
    session: SessionDep, user: CurrentUser, tenant_id: UUID, kind: str, scope_id: str
) -> tuple[TenantContext, RateScope]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    try:
        scope = RateScope.model_validate({"kind": kind, "scope_id": scope_id})
    except ValueError:
        raise HTTPException(422, "比例作用域无效") from None
    return context, scope


@router.get("/rates", response_model=RatePublic)
def rates(
    tenant_id: UUID, kind: str, scope_id: str, session: SessionDep, user: CurrentUser
) -> RatePublic:
    context, scope = _rate_context(session, user, tenant_id, kind, scope_id)
    return read_rate_schedule(session, context=context, **scope.model_dump())


@router.post("/rates/preview", response_model=RatePreviewPublic)
def preview_rate(
    tenant_id: UUID, body: RateInput, session: SessionDep, user: CurrentUser
) -> RatePreviewPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    return change_rate(session, context=context, body=body, save=False)


@router.post("/rates", response_model=RatePublic)
def set_rate(
    tenant_id: UUID, body: RateInput, session: SessionDep, user: CurrentUser
) -> RatePublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    result = change_rate(session, context=context, body=body, save=True)
    session.commit()
    return RatePublic(revision=result.revision, periods=result.periods)


@router.get("/rates/history", response_model=RateHistoryPublic)
def rate_history(
    tenant_id: UUID,
    kind: str,
    scope_id: str,
    session: SessionDep,
    user: CurrentUser,
    offset: int = Query(default=0, ge=0, le=1000000),
    limit: int = Query(default=20, ge=1, le=100),
) -> RateHistoryPublic:
    context, scope = _rate_context(session, user, tenant_id, kind, scope_id)
    read_rate_schedule(session, context=context, **scope.model_dump())
    common = [
        col(BusinessRateSchedule.tenant_id) == tenant_id,
        col(BusinessRateSchedule.kind) == scope.kind,
        col(BusinessRateSchedule.scope_id) == scope.scope_id,
    ]
    active = [*common, BusinessRateSchedule.operation["action"].astext != "migration"]
    count = session.exec(
        select(func.count()).select_from(BusinessRateSchedule).where(*active)
    ).one()
    rows = session.exec(
        select(BusinessRateSchedule)
        .where(*active)
        .order_by(col(BusinessRateSchedule.revision).desc())
        .offset(offset)
        .limit(limit + 1)
    ).all()
    items = []
    for row in rows:
        previous = session.exec(
            select(BusinessRateSchedule)
            .where(*common, col(BusinessRateSchedule.revision) < row.revision)
            .order_by(col(BusinessRateSchedule.revision).desc())
            .limit(1)
        ).first()
        items.append(
            RateHistoryItem(
                id=row.id,
                revision=row.revision,
                actor_id=row.actor_id,
                created_at=row.created_at,
                note=row.operation.get("note", ""),
                periods=[RatePeriod.model_validate(p) for p in row.periods],
                previous_periods=[
                    RatePeriod.model_validate(p) for p in previous.periods
                ]
                if previous
                else [],
            )
        )
    if len(items) <= limit:
        legacy = session.exec(
            select(BusinessRate)
            .where(
                col(BusinessRate.tenant_id) == tenant_id,
                col(BusinessRate.kind) == scope.kind,
                col(BusinessRate.scope_id) == scope.scope_id,
            )
            .order_by(col(BusinessRate.revision).desc())
            .offset(max(0, offset - count))
            .limit(limit + 1 - len(items))
        ).all()
        items.extend(
            RateHistoryItem(
                id=r.id,
                revision=r.revision,
                actor_id=r.actor_id,
                created_at=r.created_at,
                note="旧版生效日期记录",
                periods=[RatePeriod(start_date=r.effective_date, rate=r.rate)],
                previous_periods=[],
                legacy=True,
            )
            for r in legacy
        )
    visible = items[:limit]
    actors = (
        {
            actor.id: actor.username
            for actor in session.exec(
                select(User).where(
                    col(User.id).in_({item.actor_id for item in visible})
                )
            ).all()
        }
        if visible
        else {}
    )
    for item in visible:
        item.actor_name = actors.get(item.actor_id, "未知操作人")
    return RateHistoryPublic(items=visible, has_more=len(items) > limit)


@router.put("/mapping")
def mapping(
    tenant_id: UUID, body: MappingInput, session: SessionDep, user: CurrentUser
) -> dict[str, Any]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    row = save_mapping(session, context=context, **body.model_dump())
    session.commit()
    return {"drama_id": str(row.drama_id)}


@router.get("/evidence", response_model=EvidencePublic)
def read_evidence(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    bc_id: str,
    advertiser_id: str,
    campaign_id: str,
    stat_date: date,
) -> EvidencePublic:
    from app.modules.business.evidence import read_evidence as read

    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    row = read(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
        stat_date=stat_date,
    )
    return (
        EvidencePublic(bid=row.bid, eligible=row.eligible, source=row.source)
        if row
        else EvidencePublic()
    )


@router.put("/evidence")
def evidence(
    tenant_id: UUID, body: EvidenceInput, session: SessionDep, user: CurrentUser
) -> dict[str, Any]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    if body.advertiser_id not in scope_accounts(session, context, body.bc_id):
        raise HTTPException(403, "当前BC未授权账户")
    row = BusinessEvidence(**body.model_dump(), tenant_id=tenant_id, actor_id=user.id)
    session.merge(row)
    bump_scopes(session, tenant_id, body.bc_id)
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=user.id,
            action="business.evidence.update",
            target_id=f"{body.advertiser_id}:{body.campaign_id}:{body.stat_date}",
            details={
                "bc_id": body.bc_id,
                "source": body.source,
                "bid": str(body.bid),
                "eligible": body.eligible,
            },
        )
    )
    session.commit()
    return {"saved": True}


@router.put("/member")
def member(
    tenant_id: UUID, body: MemberInput, session: SessionDep, user: CurrentUser
) -> dict[str, Any]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    if body.advertiser_id not in scope_accounts(session, context, body.bc_id):
        raise HTTPException(403, "当前BC未授权账户")
    if body.end_date is not None and body.end_date < body.start_date:
        raise HTTPException(422, "成员结束日期早于开始日期")
    row = BusinessMember(**body.model_dump(), tenant_id=tenant_id)
    session.merge(row)
    bump_scopes(session, tenant_id, body.bc_id)
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=user.id,
            action="business.member.update",
            target_id=body.advertiser_id,
            details=body.model_dump(mode="json"),
        )
    )
    session.commit()
    return {"saved": True}


@router.get("/runs")
def runs(
    tenant_id: UUID, bc_id: str, session: SessionDep, user: CurrentUser
) -> list[dict[str, Any]]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    scope_accounts(session, context, bc_id)
    scope = session.get(BusinessScope, (tenant_id, bc_id))
    statement = select(BusinessRun).where(
        col(BusinessRun.tenant_id) == tenant_id, col(BusinessRun.bc_id) == bc_id
    )
    rows = session.exec(
        statement.order_by(col(BusinessRun.created_at).desc()).limit(20)
    ).all()
    return [
        {
            "id": str(r.id),
            "quarter": str(r.quarter),
            "end_date": str(r.end_date),
            "status": r.status,
            "phase": r.phase,
            "cursor": str(r.cursor),
            "rows": r.rows_count,
            "error": r.error,
            "configuration_changed": scope is not None
            and scope.revision != r.scope_revision,
        }
        for r in rows
    ]


@router.post("/exports", response_model=ExportPublic, status_code=202)
def export(
    tenant_id: UUID, body: DashboardFilter, session: SessionDep, user: CurrentUser
) -> ExportPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    row = create_export(session, context=context, filters=body)
    session.commit()
    return ExportPublic(id=row.id, status=row.status, expires_at=row.expires_at)


@router.get("/exports/{export_id}", response_model=ExportPublic)
def export_status(
    tenant_id: UUID, export_id: UUID, session: SessionDep, user: CurrentUser
) -> ExportPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    row = check_export(session, context=context, export_id=export_id)
    return ExportPublic(id=row.id, status=row.status, expires_at=row.expires_at)


@router.get("/exports/{export_id}/download")
def download(
    tenant_id: UUID, export_id: UUID, session: SessionDep, user: CurrentUser
) -> StreamingResponse:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    return StreamingResponse(
        export_chunks(session, context=context, export_id=export_id),
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="business-{export_id}.csv"',
        },
    )
