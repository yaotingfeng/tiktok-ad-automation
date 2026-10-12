"""在本地发布数据上构建并读取短期报表快照。

查询请求只在第一次请求时读取目录/事实。后续分页和选择均消费
快照行，避免修正或改名在同一个浏览器操作中混入新版本。
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.modules.accounts.models import TenantBC
from app.modules.ads.models import AdObject, CampaignNameProjection
from app.modules.reporting.dimension_query import (
    COMPLETE_COVERAGE_STATUSES,
    build_rows,
    merge_metric_buckets,
)
from app.modules.reporting.filters import authorized_grants, compile_filter
from app.modules.reporting.query_models import (
    QuerySnapshot,
    QuerySnapshotRow,
    read_snapshot,
    read_snapshot_rows,
    snapshot_transaction,
)
from app.modules.reporting.schemas import (
    AdsQueryPage,
    MetricVector,
    QuerySnapshotPublic,
    ReportingFilter,
    ReportRow,
)
from app.modules.reporting.store_models import ReportSyncState
from app.modules.tenants.permissions import require_tenant

SNAPSHOT_INSERT_BATCH = 1000


def filter_digest(filters: ReportingFilter) -> str:
    """Canonical filter digest used to bind cursors and snapshots."""
    payload = json.dumps(
        filters.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> dict[str, Any]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(404, detail="query_cursor_not_found") from exc


def _row_public(row: QuerySnapshotRow, *, for_listing: bool = False) -> ReportRow:
    display = dict(row.display)
    # B1's row table predates the public coverage field. Keep it in the JSON
    # display payload so the migration remains compatible while snapshots retain
    # the exact coverage state captured at creation time.
    coverage = display.pop("__coverage__", {})
    # 快照内的账户行保留其下全部系列引用，供冻结选择展开；列表响应不返回这些引用，
    # 避免每个账户行带回上百条。冻结选择调用时 for_listing 保持 False。
    refs = () if for_listing and row.dimension == "account" else row.refs
    return ReportRow.model_validate(
        {
            "row_key": row.row_key,
            "display": display,
            "refs": refs,
            "material_uses": row.material_uses,
            "metric_buckets": row.metric_buckets,
            "capabilities": row.capabilities,
            "directory_versions": row.directory_versions,
            "membership_digest": row.membership_digest,
            "coverage": coverage,
        }
    )


def _summary(rows: tuple[ReportRow, ...]) -> dict[str, Any]:
    """汇总按币种分桶，与行内合并共用部分合计规则（merge_metric_buckets）。

    混口径/混币种桶仍按桶展示，不偷偷换汇。缺覆盖的成员不当成 0：已同步部分照常
    相加并标 PARTIAL，同时给出不完整账户数与缺失日期，由页面提示"N 个账户数据
    不完整"。
    """
    incomplete_advertisers: set[str] = set()
    incomplete_dates: set[str] = set()
    members: list[tuple[tuple[MetricVector, ...], bool]] = []
    for row in rows:
        incomplete = row.coverage.get("status") not in COMPLETE_COVERAGE_STATUSES
        if incomplete:
            # 剧维度一行合并多个账户，覆盖里列出了具体哪些账户不完整。
            incomplete_advertisers.update(
                cast(list[str], row.coverage.get("incomplete_advertiser_ids") or [])
            )
            incomplete_dates.update(
                cast(list[str], row.coverage.get("stale_dates") or [])
            )
        members.append((row.metric_buckets, incomplete))
    return {
        "row_count": len(rows),
        "buckets": [
            total.model_dump(mode="json") for total in merge_metric_buckets(members)
        ],
        "incomplete_advertiser_count": len(incomplete_advertisers),
        "incomplete_dates": sorted(incomplete_dates),
    }


def _snapshot_public(snapshot: QuerySnapshot) -> QuerySnapshotPublic:
    return QuerySnapshotPublic(
        snapshot_id=snapshot.id,
        expires_at=snapshot.expires_at,
        filters=ReportingFilter.model_validate(snapshot.filters),
        publication_versions=snapshot.publication_versions,
    )


def _check_cursor(
    *,
    cursor: str,
    snapshot: QuerySnapshot,
    filters: ReportingFilter,
    context: TenantContext,
    bc_id: str,
) -> int:
    payload = _decode_cursor(cursor)
    expected = {
        "snapshot_id": str(snapshot.id),
        "filter_digest": snapshot.filter_digest,
        "tenant_id": str(context.tenant_id),
        "actor_id": str(context.actor_id),
        "bc_id": bc_id,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise HTTPException(404, detail="query_cursor_not_found")
    if snapshot.filter_digest != filter_digest(filters):
        raise HTTPException(409, detail="query_cursor_filter_mismatch")
    sequence = payload.get("sequence")
    if type(sequence) is not int or sequence < 0:
        raise HTTPException(404, detail="query_cursor_not_found")
    return sequence


def _new_snapshot(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    rows: tuple[ReportRow, ...],
) -> QuerySnapshot:
    grants = authorized_grants(
        session, context=context, bc_id=bc_id, advertiser_ids=filters.advertiser_ids
    )
    if filters.advertiser_ids and {item.advertiser_id for item in grants} != set(
        filters.advertiser_ids
    ):
        raise HTTPException(403, detail="report_scope_forbidden")
    allowed_ids = {item.advertiser_id for item in grants}
    now = datetime.now(UTC)
    statuses = [row.coverage.get("status") for row in rows]
    publication_versions = {"reporting": 1}
    updated = session.exec(
        select(func.max(ReportSyncState.last_success_at)).where(
            ReportSyncState.tenant_id == context.tenant_id,
            col(ReportSyncState.advertiser_id).in_(allowed_ids),
            ReportSyncState.stat_date >= filters.start_date,
            ReportSyncState.stat_date <= filters.end_date,
        )
    ).one()
    if updated is not None:
        publication_versions["max"] = int(updated.timestamp() * 1_000_000)
    naming_versions = {"directory": 1}
    campaigns = {
        (ref.advertiser_id, ref.remote_id)
        for row in rows
        for ref in row.refs
        if ref.kind == "campaign"
    }
    if campaigns:
        projections = session.exec(
            select(CampaignNameProjection).where(
                CampaignNameProjection.tenant_id == context.tenant_id,
                col(CampaignNameProjection.advertiser_id).in_(
                    {item[0] for item in campaigns}
                ),
                col(CampaignNameProjection.campaign_remote_id).in_(
                    {item[1] for item in campaigns}
                ),
            )
        ).all()
        for projection in projections:
            naming_versions[
                f"{projection.advertiser_id}:{projection.campaign_remote_id}"
            ] = projection.name_revision
    snapshot = QuerySnapshot(
        id=uuid4(),
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        actor_id=context.actor_id,
        advertiser_ids=sorted(item.advertiser_id for item in grants),
        filters=filters.model_dump(mode="json"),
        filter_digest=filter_digest(filters),
        publication_versions=publication_versions,
        naming_versions=naming_versions,
        expires_at=now + timedelta(minutes=15),
        total=len(rows),
        summary=_summary(rows),
        coverage={
            "status": "EMPTY"
            if not rows
            else (
                "COMPLETE"
                if all(status in COMPLETE_COVERAGE_STATUSES for status in statuses)
                else "INCOMPLETE"
            ),
            "rows": len(rows),
        },
    )
    snapshot.trends = {}
    session.add(snapshot)
    session.flush()
    for sequence, row in enumerate(rows):
        if sequence and sequence % SNAPSHOT_INSERT_BATCH == 0:
            # 素材维度单次快照可达数万行 JSONB；一次性 executemany 会让数据库代理
            # 在写入途中断开连接，按批 flush 控制单次往返的数据量。
            session.flush()
        encoded = row.model_dump(mode="json")
        session.add(
            QuerySnapshotRow(
                snapshot_id=snapshot.id,
                row_key=row.row_key,
                stable_sequence=sequence,
                dimension=filters.dimension,
                display=encoded["display"] | {"__coverage__": encoded["coverage"]},
                refs=encoded["refs"],
                material_uses=encoded["material_uses"],
                metric_buckets=encoded["metric_buckets"],
                capabilities=encoded["capabilities"],
                directory_versions=encoded["directory_versions"],
                membership_digest=row.membership_digest,
            )
        )
    session.flush()
    return snapshot


def _read_page(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    snapshot: QuerySnapshot,
    cursor: str | None,
    limit: int,
) -> AdsQueryPage:
    after = (
        -1
        if cursor is None
        else _check_cursor(
            cursor=cursor,
            snapshot=snapshot,
            filters=filters,
            context=context,
            bc_id=bc_id,
        )
    )
    rows = read_snapshot_rows(
        session,
        context=context,
        bc_id=bc_id,
        snapshot_id=snapshot.id,
        after_sequence=after,
        limit=limit,
    )
    items = tuple(_row_public(row, for_listing=True) for row in rows)
    next_cursor = None
    if rows and rows[-1].stable_sequence + 1 < snapshot.total:
        next_cursor = _cursor(
            {
                "snapshot_id": str(snapshot.id),
                "filter_digest": snapshot.filter_digest,
                "tenant_id": str(context.tenant_id),
                "actor_id": str(context.actor_id),
                "bc_id": bc_id,
                "sequence": rows[-1].stable_sequence,
            }
        )
    return AdsQueryPage(
        snapshot=_snapshot_public(snapshot),
        items=items,
        total=snapshot.total,
        summary=snapshot.summary,
        coverage=snapshot.coverage,
        next_cursor=next_cursor,
    )


def _build_snapshot(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
) -> QuerySnapshot:
    rows = build_rows(session, context=context, bc_id=bc_id, filters=filters)
    # Account rows are report aggregates, but management selection must retain
    # the exact campaign refs visible at snapshot creation time. The query's
    # authorized account set is fixed once, and one batch query avoids N+1 reads.
    frozen_rows: list[ReportRow] = []
    if filters.dimension == "account":
        grants = authorized_grants(
            session, context=context, bc_id=bc_id, advertiser_ids=filters.advertiser_ids
        )
        allowed_ids = {item.advertiser_id for item in grants}
        campaigns = session.exec(
            select(AdObject)
            .where(
                AdObject.tenant_id == context.tenant_id,
                col(AdObject.advertiser_id).in_(allowed_ids),
                AdObject.kind == "campaign",
            )
            .order_by(AdObject.advertiser_id, AdObject.remote_id)
        ).all()
        by_account: dict[str, list[AdObject]] = {}
        for campaign in campaigns:
            by_account.setdefault(campaign.advertiser_id, []).append(campaign)
        for row in rows:
            advertiser_id = row.display.get("remote_id")
            frozen_rows.append(
                row.model_copy(
                    update={
                        "refs": tuple(
                            item.ref for item in by_account.get(advertiser_id, [])
                        )
                    }
                )
            )
        rows = tuple(frozen_rows)
    snapshot = _new_snapshot(
        session, context=context, bc_id=bc_id, filters=filters, rows=rows
    )
    # 趋势已从报表页移除。快照只保存列表和汇总，避免首次打开页面重复扫描
    # 全量事实。
    snapshot.trends = {}
    session.flush()
    return snapshot


def query_ads(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    snapshot_id: UUID | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> AdsQueryPage:
    """查询本地快照，分页游标始终绑定租户、BC、用户和筛选摘要。"""
    if not 1 <= limit <= 100:
        raise HTTPException(422, detail="invalid_snapshot_page")
    try:
        compile_filter(filters)
    except ValueError as exc:
        raise HTTPException(422, detail="invalid_reporting_filter") from exc
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    if snapshot_id is None:
        if cursor is not None:
            raise HTTPException(404, detail="query_cursor_not_found")
        # 先验证 BC 存在且当前操作者至少有一个可读账户，再创建快照。
        # 否则未知/未授权 BC 会先写入带无效外键的空快照，造成 500，
        # 同时把 BC 是否存在泄露给调用方。对外统一隐藏为 404。
        bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
        if bc is None or bc.ownership_conflict:
            raise HTTPException(404, detail="query_scope_not_found")
        grants = authorized_grants(
            session, context=context, bc_id=bc_id, advertiser_ids=filters.advertiser_ids
        )
        if not grants:
            raise HTTPException(404, detail="query_scope_not_found")
        if session.get_bind() is engine:
            with snapshot_transaction(engine) as snapshot_session:
                snapshot = _build_snapshot(
                    snapshot_session, context=context, bc_id=bc_id, filters=filters
                )
        else:
            snapshot = _build_snapshot(
                session, context=context, bc_id=bc_id, filters=filters
            )
        snapshot = read_snapshot(
            session, context=context, bc_id=bc_id, snapshot_id=snapshot.id
        )
    else:
        snapshot = read_snapshot(
            session, context=context, bc_id=bc_id, snapshot_id=snapshot_id
        )
        if snapshot.actor_id != context.actor_id:
            raise HTTPException(404, detail="query_snapshot_not_found")
        if snapshot.filter_digest != filter_digest(filters):
            raise HTTPException(409, detail="query_snapshot_filter_mismatch")
    return _read_page(
        session,
        context=context,
        bc_id=bc_id,
        filters=filters,
        snapshot=snapshot,
        cursor=cursor,
        limit=limit,
    )
