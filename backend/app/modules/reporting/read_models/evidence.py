"""查询快照的不可变来源证据；头和快照均以外键引用。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ForeignKeyConstraint, Index, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import AwareDateTime


class ReportEvidenceVersion(SQLModel, table=True):
    __tablename__ = "report_evidence_version"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        UniqueConstraint(
            "tenant_id", "advertiser_id", "id", name="uq_report_evidence_scope"
        ),
        Index(
            "ix_report_evidence_retired",
            "retired_at",
            postgresql_where=text("retired_at IS NOT NULL"),
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    advertiser_id: str = Field(max_length=128)
    kind: str = Field(max_length=32)
    refs: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    material_uses: list[dict[str, Any]] = Field(
        default_factory=list, sa_type=JSONB, nullable=False
    )
    naming_versions: dict[str, int] = Field(
        default_factory=dict, sa_type=JSONB, nullable=False
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime
    )
    retired_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class ReportNameScopeCurrent(SQLModel, table=True):
    __tablename__ = "report_name_scope_current"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id"],
            ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id", "version_id"],
            [
                "report_evidence_version.tenant_id",
                "report_evidence_version.advertiser_id",
                "report_evidence_version.id",
            ],
        ),
        Index("ix_report_name_scope_version", "version_id"),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    version_id: UUID


class QuerySnapshotEvidence(SQLModel, table=True):
    __tablename__ = "query_snapshot_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "row_key"],
            ["query_snapshot_row.snapshot_id", "query_snapshot_row.row_key"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["version_id"], ["report_evidence_version.id"]),
        Index("ix_query_snapshot_evidence_version", "version_id"),
    )
    snapshot_id: UUID = Field(primary_key=True)
    row_key: str = Field(primary_key=True, max_length=512)
    version_id: UUID = Field(primary_key=True)


class QuerySnapshotNameVersion(SQLModel, table=True):
    __tablename__ = "query_snapshot_name_version"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id"], ["query_snapshot.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["version_id"], ["report_evidence_version.id"]),
        Index("ix_query_snapshot_name_version_version", "version_id"),
    )
    snapshot_id: UUID = Field(primary_key=True)
    version_id: UUID = Field(primary_key=True)
