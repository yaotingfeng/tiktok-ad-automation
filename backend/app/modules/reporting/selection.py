"""跨页选择冻结。

``ALL_MATCHING`` 只展开来源快照，绝不重新执行筛选；因此新同步行和改名归组
不会悄悄进入已提交的管理目标。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.modules.reporting.filters import authorized_grants
from app.modules.reporting.queries import _row_public
from app.modules.reporting.query_models import (
    FrozenSelectionRecord,
    QuerySnapshotRow,
    read_snapshot,
)
from app.modules.reporting.schemas import FrozenSelection, SelectionRequest
from app.modules.tenants.permissions import require_tenant


def _digest(refs: tuple[EntityRef, ...], uses: tuple[MaterialUseRef, ...]) -> str:
    values = {
        "refs": [
            (str(ref.tenant_id), ref.advertiser_id, ref.kind, ref.remote_id)
            for ref in refs
        ],
        "uses": [
            (
                str(item.ad_ref.tenant_id),
                item.ad_ref.advertiser_id,
                item.ad_ref.remote_id,
                item.platform_material_id,
                item.ad_material_id,
                item.material_type,
            )
            for item in uses
        ],
    }
    return sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _entity(data: dict[str, Any]) -> EntityRef:
    return EntityRef(
        UUID(str(data["tenant_id"])),
        str(data["advertiser_id"]),
        data["kind"],
        str(data["remote_id"]),
    )


def _usage(data: dict[str, Any]) -> MaterialUseRef:
    return MaterialUseRef(
        _entity(data["ad_ref"]),
        str(data["platform_material_id"]),
        data.get("ad_material_id"),
        str(data["material_type"]),
    )


def _all_rows(session: Session, *, snapshot_id: UUID, total: int) -> tuple[Any, ...]:
    rows: list[QuerySnapshotRow] = []
    after = -1
    while len(rows) < total:
        batch = tuple(
            session.exec(
                select(QuerySnapshotRow)
                .where(
                    QuerySnapshotRow.snapshot_id == snapshot_id,
                    col(QuerySnapshotRow.stable_sequence) > after,
                )
                .order_by(QuerySnapshotRow.stable_sequence)
                .limit(100)
            ).all()
        )
        if not batch:
            break
        rows.extend(batch)
        after = batch[-1].stable_sequence
    return tuple(rows)


def _selection_public(record: FrozenSelectionRecord) -> FrozenSelection:
    refs = tuple(_entity(item) for item in record.refs)
    uses = tuple(_usage(item) for item in record.material_uses)
    return FrozenSelection(
        selection_id=record.id,
        snapshot_id=record.snapshot_id,
        refs=refs,
        material_uses=uses,
        membership_digest=record.membership_digest,
        expires_at=record.expires_at,
    )


def freeze_selection(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    request: SelectionRequest,
) -> FrozenSelection:
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read")
    if set(request.row_keys) & set(request.excluded_row_keys):
        raise HTTPException(422, detail="selection_row_conflict")
    snapshot = read_snapshot(session, context=context, bc_id=bc_id, snapshot_id=request.snapshot_id)
    if snapshot.actor_id != context.actor_id:
        raise HTTPException(404, detail="query_snapshot_not_found")
    rows = _all_rows(session, snapshot_id=snapshot.id, total=snapshot.total)
    by_key = {row.row_key: row for row in rows}
    if request.mode == "EXPLICIT":
        unknown = set(request.row_keys) - set(by_key)
        if unknown:
            raise HTTPException(404, detail="selection_row_not_found")
        selected = [by_key[key] for key in request.row_keys]
    else:
        excluded = set(request.excluded_row_keys)
        selected = [row for row in rows if row.row_key not in excluded]
    refs: list[EntityRef] = []
    uses: list[MaterialUseRef] = []
    seen_refs: set[tuple[Any, ...]] = set()
    seen_uses: set[tuple[Any, ...]] = set()
    for row in selected:
        public = _row_public(row)
        # Account refs were expanded when the query snapshot was created; reading
        # current AdObject rows here would admit campaigns added after the snapshot.
        row_refs = list(public.refs)
        for ref in row_refs:
            key = (ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id)
            if key not in seen_refs:
                seen_refs.add(key)
                refs.append(ref)
        for use in public.material_uses:
            key = (
                use.ad_ref.tenant_id,
                use.ad_ref.advertiser_id,
                use.ad_ref.remote_id,
                use.platform_material_id,
                use.ad_material_id,
                use.material_type,
            )
            if key not in seen_uses:
                seen_uses.add(key)
                uses.append(use)
    refs_tuple, uses_tuple = tuple(refs), tuple(uses)
    now = datetime.now(UTC)
    record = FrozenSelectionRecord(
        id=uuid4(),
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        actor_id=context.actor_id,
        advertiser_ids=list(snapshot.advertiser_ids),
        filters=snapshot.filters,
        filter_digest=snapshot.filter_digest,
        publication_versions=snapshot.publication_versions,
        naming_versions=snapshot.naming_versions,
        expires_at=min(snapshot.expires_at, now + timedelta(minutes=15)),
        snapshot_id=snapshot.id,
        refs=[
            {
                "tenant_id": str(ref.tenant_id),
                "advertiser_id": ref.advertiser_id,
                "kind": ref.kind,
                "remote_id": ref.remote_id,
            }
            for ref in refs_tuple
        ],
        material_uses=[
            {
                "ad_ref": {
                    "tenant_id": str(use.ad_ref.tenant_id),
                    "advertiser_id": use.ad_ref.advertiser_id,
                    "kind": use.ad_ref.kind,
                    "remote_id": use.ad_ref.remote_id,
                },
                "platform_material_id": use.platform_material_id,
                "ad_material_id": use.ad_material_id,
                "material_type": use.material_type,
            }
            for use in uses_tuple
        ],
        membership_digest=_digest(refs_tuple, uses_tuple),
    )
    session.add(record)
    session.flush()
    return _selection_public(record)


def get_frozen_selection(
    session: Session, *, context: TenantContext, bc_id: str, selection_id: UUID
) -> FrozenSelection:
    """C 读取不可变目标；每次读取重新校验当前权限和有效期。"""
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read")
    record = session.exec(
        select(FrozenSelectionRecord).where(
            FrozenSelectionRecord.id == selection_id,
            FrozenSelectionRecord.tenant_id == context.tenant_id,
            FrozenSelectionRecord.bc_id == bc_id,
            FrozenSelectionRecord.actor_id == context.actor_id,
        )
    ).first()
    if record is None:
        raise HTTPException(404, detail="frozen_selection_not_found")
    grants = authorized_grants(session, context=context, bc_id=bc_id)
    if not set(record.advertiser_ids) <= {grant.advertiser_id for grant in grants}:
        raise HTTPException(403, detail="report_scope_forbidden")
    if record.expires_at <= datetime.now(UTC):
        raise HTTPException(409, detail="frozen_selection_expired")
    return _selection_public(record)
