"""派生金额的覆盖与观察时点随桶保存，不能借新源状态把旧/缺失投影称作最新。"""

import hashlib
import json
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import select as sql_select
from sqlmodel import Session, col, select

from app.modules.reporting.read_models.models import ReadBucketState
from app.modules.reporting.store_models import ReportSyncState


class ProjectionCoverageRow(Protocol):
    """覆盖与digest只依赖这五列；ORM对象和窄列Row均遵守同一读取合同。"""

    @property
    def advertiser_id(self) -> str: ...

    @property
    def bucket(self) -> date: ...

    @property
    def source_revision(self) -> int: ...

    @property
    def derived_revision(self) -> int: ...

    @property
    def derived_observed_at(self) -> datetime | None: ...


def read_projection_coverage(
    session: Session,
    *,
    tenant_id: UUID,
    advertisers: Sequence[str],
    start: date,
    end: date,
    kind: str = "material_day",
) -> dict[str, Any]:
    """全局覆盖读取窄列，不构建并未使用的同步状态或SQLModel对象。"""
    if not advertisers:
        return projection_coverage([])
    rows = (
        session.connection()
        .execute(
            sql_select(
                col(ReadBucketState.advertiser_id),
                col(ReadBucketState.bucket),
                col(ReadBucketState.source_revision),
                col(ReadBucketState.derived_revision),
                col(ReadBucketState.derived_observed_at),
            ).where(
                col(ReadBucketState.tenant_id) == tenant_id,
                col(ReadBucketState.advertiser_id).in_(advertisers),
                col(ReadBucketState.kind) == kind,
                col(ReadBucketState.bucket) >= start,
                col(ReadBucketState.bucket) <= end,
            )
        )
        .all()
    )
    # SQL列名和类型与Protocol逐项对应；保留driver的日期/时区解码及原digest序列化。
    return projection_coverage(cast(Sequence[ProjectionCoverageRow], rows))


def projected_states(
    session: Session,
    *,
    tenant_id: UUID,
    advertisers: Sequence[str],
    start: date,
    end: date,
    kind: str = "material_day",
) -> tuple[list[ReportSyncState], Sequence[ReadBucketState]]:
    rows = session.exec(
        select(ReadBucketState).where(
            ReadBucketState.tenant_id == tenant_id,
            col(ReadBucketState.advertiser_id).in_(advertisers),
            ReadBucketState.kind == kind,
            ReadBucketState.bucket >= start,
            ReadBucketState.bucket <= end,
        )
    ).all()
    states = [
        ReportSyncState(
            tenant_id=row.tenant_id,
            advertiser_id=row.advertiser_id,
            report_kind=kind,
            stat_date=row.bucket,
            status=row.derived_status or "STALE",
            last_success_at=row.derived_observed_at,
            observed_at=row.derived_observed_at,
            publication_version=row.derived_revision,
        )
        for row in rows
    ]
    return states, rows


def projection_coverage(rows: Sequence[ProjectionCoverageRow]) -> dict[str, Any]:
    dirty = [row for row in rows if row.source_revision > row.derived_revision]
    missing = [
        row for row in rows if row.derived_revision == 0 and row.source_revision > 0
    ]
    observed = [row.derived_observed_at for row in rows if row.derived_observed_at]

    def digest(column: str) -> str:
        values = sorted(
            (row.advertiser_id, str(row.bucket), getattr(row, column)) for row in rows
        )
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    return {
        "status": "BUILDING" if missing else "STALE" if dirty else "CURRENT",
        "pending_bucket_count": len(dirty),
        "unavailable_bucket_count": len(missing),
        "metrics_observed_at": max(observed).isoformat() if observed else None,
        "source_digest": digest("source_revision"),
        "derived_digest": digest("derived_revision"),
    }
