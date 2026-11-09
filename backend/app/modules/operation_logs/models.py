"""平台事实按租户账户保存，经营消费冻结不可变ID而不是活跃行。"""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import AwareDateTime


def now() -> datetime:
    return datetime.now(UTC)


class OperationExport(SQLModel, table=True):
    __tablename__ = "operation_export"
    __table_args__ = (
        Index(
            "ix_operation_export_account",
            "tenant_id",
            "advertiser_id",
            "start_at",
            "end_at",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    advertiser_id: str = Field(max_length=128)
    bc_id: str = Field(max_length=128)
    frozen_route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    task_id: str = Field(max_length=128)
    start_at: datetime = Field(sa_type=AwareDateTime)
    end_at: datetime = Field(sa_type=AwareDateTime)
    timezone: str = Field(max_length=128)
    observed_at: datetime = Field(sa_type=AwareDateTime)
    digest: str = Field(max_length=64)
    archive_key: str
    byte_length: int
    parser_version: int
    event_count: int


class OperationLogEvent(SQLModel, table=True):
    __tablename__ = "operation_log_event"
    export_id: UUID = Field(primary_key=True, foreign_key="operation_export.id")
    identity: str = Field(primary_key=True, max_length=96)
    occurred_at: datetime = Field(sa_type=AwareDateTime)
    object_type: str = Field(max_length=32)
    object_id: str = Field(max_length=128)
    details: list[dict[str, Any]] = Field(sa_column=Column(JSONB, nullable=False))
    source: str
    attribution: str = Field(max_length=16)


class OperationCoverage(SQLModel, table=True):
    __tablename__ = "operation_coverage"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id"],
            ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    stat_date: date = Field(primary_key=True)
    export_id: UUID = Field(foreign_key="operation_export.id")
    digest: str = Field(max_length=64)
    checked_at: datetime = Field(sa_type=AwareDateTime)
    revision: int = 1


class OperationObservation(SQLModel, table=True):
    __tablename__ = "operation_observation"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "advertiser_id",
            "kind",
            "remote_id",
            "digest",
            "observation_day",
            name="uq_operation_observation",
        ),
        Index(
            "ix_operation_observation_account",
            "tenant_id",
            "advertiser_id",
            "observed_at",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    advertiser_id: str = Field(max_length=128)
    kind: str = Field(max_length=32)
    remote_id: str = Field(max_length=128)
    parent_id: str | None = Field(default=None, max_length=128)
    observed_at: datetime = Field(sa_type=AwareDateTime)
    observation_day: date
    digest: str = Field(max_length=64)
    configuration: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    source: str = Field(default="OFFICIAL_DIRECTORY", max_length=32)


class OperationDayEvidence(SQLModel, table=True):
    __tablename__ = "operation_day_evidence"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "advertiser_id",
            "campaign_id",
            "stat_date",
            "digest",
            name="uq_operation_day_evidence",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    advertiser_id: str = Field(max_length=128)
    campaign_id: str = Field(max_length=128)
    stat_date: date
    digest: str = Field(max_length=64)
    data: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(default_factory=now, sa_type=AwareDateTime)


class OperationSyncCursor(SQLModel, table=True):
    __tablename__ = "operation_sync_cursor"
    tenant_id: UUID = Field(primary_key=True, foreign_key="tenant.id")
    advertiser_id: str = Field(primary_key=True, max_length=128)
    recent_day: date | None = None
    history_cursor: date | None = None
    history_sweep_day: date | None = None
