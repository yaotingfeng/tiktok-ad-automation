"""在本地发布数据上构建并读取短期报表快照。

查询请求只在第一次请求时读取目录/事实。后续分页、趋势和选择均消费
快照行，避免修正或改名在同一个浏览器操作中混入新版本。
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.modules.ads.models import AdObject
from app.modules.reporting.aggregation import aggregate_metrics, build_dimension_rows
from app.modules.reporting.filters import authorized_grants, compile_filter
from app.modules.reporting.query_models import (
    QuerySnapshot,
    QuerySnapshotRow,
    read_snapshot,
    read_snapshot_rows,
)
from app.modules.reporting.schemas import (
    AdsQueryPage,
    MetricVector,
    QuerySnapshotPublic,
    ReportingFilter,
    ReportRow,
    TrendPublic,
)
from app.modules.reporting.trends import build_trend
from app.modules.tenants.permissions import require_tenant


def filter_digest(filters: ReportingFilter) -> str:
    """Canonical filter digest used to bind cursors and snapshots."""
    payload = json.dumps(filters.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
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


def _row_public(row: QuerySnapshotRow) -> ReportRow:
    display = dict(row.display)
    # B1's row table predates the public coverage field. Keep it in the JSON
    # display payload so the migration remains compatible while snapshots retain
    # the exact coverage state captured at creation time.
    coverage = display.pop("__coverage__", {})
    return ReportRow.model_validate(
        {
            "row_key": row.row_key,
            "display": display,
            "refs": row.refs,
            "material_uses": row.material_uses,
            "metric_buckets": row.metric_buckets,
            "capabilities": row.capabilities,
            "directory_versions": row.directory_versions,
            "membership_digest": row.membership_digest,
            "coverage": coverage,
        }
    )


def _summary(rows: tuple[ReportRow, ...]) -> dict[str, Any]:
    """保存可重复的合计；混口径/混币种桶仍按桶展示，不偷偷换汇。"""
    buckets: dict[tuple[Any, ...], list[MetricVector]] = {}
    for row in rows:
        for vector in row.metric_buckets:
            key = (
                vector.currency,
                vector.timezone,
                vector.attribution,
                tuple(
                    sorted(
                        (name, state)
                        for name, state in vector.availability.items()
                        if name != "d0_roas"
                    )
                ),
            )
            buckets.setdefault(key, []).append(vector)
    totals: list[dict[str, Any]] = []
    for vectors in buckets.values():
        total = aggregate_metrics(vectors)
        totals.append(total.model_dump(mode="json"))
    totals.sort(key=lambda value: json.dumps(value, sort_keys=True))
    return {"row_count": len(rows), "buckets": totals}


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
    if filters.advertiser_ids and {item.advertiser_id for item in grants} != set(filters.advertiser_ids):
        raise HTTPException(403, detail="report_scope_forbidden")
    now = datetime.now(UTC)
    statuses = [row.coverage.get("status") for row in rows]
    publication_versions = {
        str(version): int(version)
        for version in {
            int(value)
            for row in rows
            for value in row.coverage.get("published_versions", [])
            if str(value).isdigit()
        }
    }
    publication_versions.setdefault("reporting", 1)
    naming_versions = {
        key: int(value)
        for row in rows
        for key, value in row.directory_versions.items()
        if isinstance(value, int)
    }
    naming_versions.setdefault("directory", 1)
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
            "status": "EMPTY" if not rows else ("COMPLETE" if all(status == "COMPLETE" for status in statuses) else "INCOMPLETE"),
            "rows": len(rows),
        },
    )
    snapshot.trends = {}
    session.add(snapshot)
    session.flush()
    for sequence, row in enumerate(rows):
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
    after = -1 if cursor is None else _check_cursor(
        cursor=cursor, snapshot=snapshot, filters=filters, context=context, bc_id=bc_id
    )
    rows = read_snapshot_rows(
        session,
        context=context,
        bc_id=bc_id,
        snapshot_id=snapshot.id,
        after_sequence=after,
        limit=limit,
    )
    items = tuple(_row_public(row) for row in rows)
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
    compile_filter(filters)
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read")
    if snapshot_id is None:
        if cursor is not None:
            raise HTTPException(404, detail="query_cursor_not_found")
        rows = build_dimension_rows(session, context=context, bc_id=bc_id, filters=filters)
        # Account rows are report aggregates, but management selection must retain
        # the exact campaign refs visible at snapshot creation time.
        frozen_rows: list[ReportRow] = []
        for row in rows:
            if filters.dimension == "account":
                advertiser_id = row.display.get("remote_id")
                campaigns = session.exec(
                    select(AdObject).where(
                        AdObject.tenant_id == context.tenant_id,
                        AdObject.advertiser_id == advertiser_id,
                        AdObject.kind == "campaign",
                    ).order_by(AdObject.remote_id)
                ).all()
                row = row.model_copy(update={"refs": tuple(item.ref for item in campaigns)})
            frozen_rows.append(row)
        rows = tuple(frozen_rows)
        snapshot = _new_snapshot(session, context=context, bc_id=bc_id, filters=filters, rows=rows)
        # Trends are best-effort local data. A missing observation is represented in
        # the stored snapshot and never triggers a provider request.
        try:
            snapshot.trends = build_trend(
                session, context=context, bc_id=bc_id, filters=filters, grain="day"
            ).model_dump(mode="json")
        except (ValueError, RuntimeError):
            snapshot.trends = TrendPublic(coverage={"status": "UNAVAILABLE"}).model_dump(mode="json")
        session.flush()
    else:
        snapshot = read_snapshot(session, context=context, bc_id=bc_id, snapshot_id=snapshot_id)
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


def snapshot_trend(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    snapshot_id: UUID,
    filters: ReportingFilter,
) -> TrendPublic:
    """读取查询快照内的趋势，不把新观测覆盖到旧报表。"""
    snapshot = read_snapshot(session, context=context, bc_id=bc_id, snapshot_id=snapshot_id)
    if snapshot.actor_id != context.actor_id:
        raise HTTPException(404, detail="query_snapshot_not_found")
    if snapshot.filter_digest != filter_digest(filters):
        raise HTTPException(409, detail="query_snapshot_filter_mismatch")
    return TrendPublic.model_validate(snapshot.trends or {"coverage": {"status": "EMPTY"}})
