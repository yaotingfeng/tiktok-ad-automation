"""报表事实与覆盖分开保存；金额不依赖目录齐全，也不跨指标族隐式求和。"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import AwareDateTime, account_reference

# 普通应用导入模型也注册运行表的复合外键，不能依赖 Alembic 进程。
from app.modules.reporting import sync_models as sync_models

# Fact 的唯一键不含 BC、通道和请求分片，重叠查询始终覆盖同一指标事实。
FACT_IDENTITY = (
    "tenant_id",
    "advertiser_id",
    "subject_key",
    "bucket_start",
    "bucket_end",
    "granularity",
    "report_contract",
    "metric_family",
    "currency",
    "timezone",
    "attribution",
    "metric_name",
)


class ReportCoordinates(SQLModel):
    tenant_id: UUID
    advertiser_id: str = Field(max_length=128)
    bucket_start: datetime = Field(sa_type=AwareDateTime)
    bucket_end: datetime = Field(sa_type=AwareDateTime)
    granularity: str = Field(max_length=32)
    report_contract: str = Field(max_length=128)
    metric_family: str = Field(max_length=64)
    currency: str = Field(max_length=16)
    timezone: str = Field(max_length=128)
    attribution: str = Field(max_length=128)


class ReportFact(ReportCoordinates, table=True):
    __tablename__ = "report_fact"
    __table_args__ = (
        account_reference(),
        CheckConstraint(
            "jsonb_typeof(attributes) = 'object'", name="ck_report_fact_attributes"
        ),
        UniqueConstraint(*FACT_IDENTITY, name="uq_report_fact_identity"),
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id", "source_run_id"],
            [
                "report_sync_run.tenant_id",
                "report_sync_run.advertiser_id",
                "report_sync_run.id",
            ],
        ),
        CheckConstraint(
            "bucket_start < bucket_end AND published_version > 0 AND request_sequence > 0",
            name="ck_report_fact_versions",
        ),
        CheckConstraint(
            "jsonb_typeof(subject_key) = 'array' AND jsonb_array_length(subject_key) > 0",
            name="ck_report_fact_subject",
        ),
        CheckConstraint(
            "value IS NULL OR value NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)",
            name="ck_report_fact_finite",
        ),
        Index(
            "ix_report_fact_period",
            "tenant_id",
            "advertiser_id",
            "report_contract",
            "metric_family",
            "bucket_start",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    subject_key: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    metric_name: str = Field(max_length=128)
    # 仅允许适配器白名单解析后的描述/关联属性，不存原始响应或下载地址。
    attributes: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    # 不指定 precision/scale，保持上游 Decimal 全精度。
    value: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(), nullable=True)
    )
    availability: str = Field(max_length=64)
    published_version: int
    request_sequence: int = Field(sa_column=Column(BigInteger, nullable=False))
    source_partition_key: str = Field(max_length=64)
    source_run_id: UUID | None = None
    observed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ReportCoverage(ReportCoordinates, table=True):
    __tablename__ = "report_coverage"
    __table_args__ = (
        account_reference(),
        UniqueConstraint(
            "tenant_id",
            "advertiser_id",
            "partition_key",
            "bucket_start",
            "bucket_end",
            name="uq_report_coverage_partition",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id", "source_run_id"],
            [
                "report_sync_run.tenant_id",
                "report_sync_run.advertiser_id",
                "report_sync_run.id",
            ],
        ),
        CheckConstraint(
            "bucket_start < bucket_end AND published_version > 0 AND request_sequence > 0",
            name="ck_report_coverage_versions",
        ),
        Index(
            "ix_report_coverage_period",
            "tenant_id",
            "advertiser_id",
            "report_contract",
            "bucket_start",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    partition_key: str = Field(max_length=64)
    # 完整空结果只影响这些明确目标和指标，不能清空同账户其他分片。
    filter_ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    requested_metrics: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    dimensions: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    status: str = Field(max_length=32)
    missing_reason: str | None = None
    observed_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    published_version: int
    request_sequence: int = Field(sa_column=Column(BigInteger, nullable=False))
    source_run_id: UUID | None = None


class ReportObservation(ReportCoordinates, table=True):
    __tablename__ = "report_observation"
    __table_args__ = (
        account_reference(),
        CheckConstraint(
            "subject_kind IN ('account','campaign')", name="ck_report_observation_kind"
        ),
        CheckConstraint(
            "bucket_start < bucket_end AND name_revision >= 0 AND grouping_revision >= 0 "
            "AND published_version > 0",
            name="ck_report_observation_versions",
        ),
        Index(
            "ix_report_observation_history",
            "tenant_id",
            "advertiser_id",
            "subject_kind",
            "observed_at",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    subject_kind: str = Field(max_length=16)
    subject_key: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    # JSON 中金额使用十进制字符串，禁止先转浮点再留档。
    values: dict[str, str | None] = Field(sa_column=Column(JSONB, nullable=False))
    availability: dict[str, str] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    observed_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    membership_digest: str = Field(max_length=64)
    name_revision: int = 0
    grouping_revision: int = 0
    published_version: int


class AccountBalanceObservation(SQLModel, table=True):
    __tablename__ = "account_balance_observation"
    __table_args__ = (
        account_reference(),
        ForeignKeyConstraint(
            ["tenant_id", "source_connection_id", "source_channel"],
            [
                "tiktok_connection.tenant_id",
                "tiktok_connection.id",
                "tiktok_connection.kind",
            ],
        ),
        CheckConstraint(
            "balance_scope IN ('ADVERTISER','PORTFOLIO','UNKNOWN')",
            name="ck_balance_scope",
        ),
        CheckConstraint(
            "(source_connection_id IS NULL) = (source_channel IS NULL)",
            name="ck_balance_source",
        ),
        CheckConstraint(
            "amount IS NULL OR amount NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)",
            name="ck_balance_finite",
        ),
        Index(
            "ix_account_balance_history", "tenant_id", "advertiser_id", "observed_at"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    advertiser_id: str = Field(max_length=128)
    amount: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(), nullable=True)
    )
    currency: str = Field(max_length=16)
    availability: str = Field(max_length=64)
    balance_scope: str = Field(default="UNKNOWN", max_length=16)
    scope_id: str | None = Field(default=None, max_length=128)
    observed_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    source_connection_id: UUID | None = None
    source_channel: str | None = Field(default=None, max_length=32)
