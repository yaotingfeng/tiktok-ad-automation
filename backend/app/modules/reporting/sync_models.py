"""报表分片发布与定时请求合同；后台身份及冻结连接随工作持久保存。"""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Identity,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from app.modules.ads.sync_models import (
    FrozenScope,
    StagedPageFields,
    SyncRunFields,
    frozen_scope_constraints,
    page_constraints,
    run_constraints,
)


class ReportSyncRun(SyncRunFields, table=True):
    __tablename__ = "report_sync_run"
    __table_args__ = run_constraints("report_sync_run")
    # 数据库序列按请求创建排序，旧运行重试不能凭较晚完成时间覆盖新请求。
    request_sequence: int | None = Field(
        default=None,
        sa_column=Column(
            BigInteger, Identity(always=True), nullable=False, unique=True
        ),
    )
    task_id: str | None = Field(default=None, max_length=128)
    task_status: str | None = Field(default=None, max_length=32)


class ReportStagedPage(StagedPageFields, table=True):
    __tablename__ = "report_staged_page"
    __table_args__ = page_constraints("report_staged_page", "report_sync_run")
    # ReportRow.values 在 JSON 中保存 Decimal 字符串，A5 发布时恢复 Decimal。
    rows: list[dict[str, Any]] = Field(sa_column=Column(JSONB, nullable=False))
    task_id: str | None = Field(default=None, max_length=128)


class SyncSchedule(FrozenScope, table=True):
    __tablename__ = "sync_schedule"
    __table_args__ = (
        *frozen_scope_constraints("sync_schedule"),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "advertiser_id",
            "scope",
            "schedule_key",
            name="uq_sync_schedule_scope",
        ),
        CheckConstraint(
            "scope IN ('directory','active','report','balance','history','targeted')",
            name="ck_sync_schedule_scope",
        ),
        CheckConstraint(
            "(start_date IS NULL AND end_date IS NULL) OR "
            "(start_date IS NOT NULL AND end_date IS NOT NULL AND start_date <= end_date)",
            name="ck_sync_schedule_dates",
        ),
        CheckConstraint(
            "claim_generation >= 0 AND interval_seconds > 0",
            name="ck_sync_schedule_numbers",
        ),
        Index("ix_sync_schedule_due", "enabled", "next_due_at", "claimed_until"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    schedule_key: str = Field(max_length=64)
    scope: str = Field(max_length=16)
    enabled: bool = True
    interval_seconds: int = 1800
    next_due_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    start_date: date | None = None
    end_date: date | None = None
    refs: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    # 已请求/已完成历史覆盖留档，不把“已排队”冒充已成功获取。
    requested_coverage: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    completed_coverage: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    last_request_id: UUID | None = None
    claim_generation: int = 0
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error_code: str | None = None
