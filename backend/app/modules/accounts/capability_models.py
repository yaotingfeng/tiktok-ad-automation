"""Link-independent BC capability jobs and complete, tenant-bound role evidence."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class CapabilityJob(SQLModel, table=True):
    __tablename__ = "account_capability_job"
    __table_args__ = (
        UniqueConstraint("tenant_id", "bc_id", "id", name="uq_capability_job_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        Index(
            "uq_capability_active_basis",
            "tenant_id",
            "bc_id",
            "connection_id",
            "credential_revision",
            "directory_basis",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_capability_repair", "status", "repair_after", "id"),
        CheckConstraint(
            "status IN ('PENDING','COMPLETE','BLOCKED','STALE','FAILED')",
            name="ck_capability_status",
        ),
        CheckConstraint(
            "phase IN ('READ','PUBLISH','DONE')", name="ck_capability_phase"
        ),
        CheckConstraint(
            "credential_revision >= 0 AND revision >= 0 AND next_page > 0 AND seen_count >= 0 AND published_count >= 0 AND failure_count >= 0",
            name="ck_capability_counters",
        ),
        CheckConstraint(
            "(claim_token IS NULL) = (claimed_until IS NULL)",
            name="ck_capability_claim",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    bc_id: str = Field(max_length=128)
    connection_id: UUID
    actor_id: UUID = Field(foreign_key="user.id")
    credential_revision: int
    directory_basis: str = Field(max_length=64)
    status: str = Field(default="PENDING", max_length=16)
    phase: str = Field(default="READ", max_length=16)
    next_page: int = 1
    total_pages: int | None = None
    total_count: int | None = None
    seen_count: int = 0
    publish_after: str | None = Field(default=None, max_length=128)
    published_count: int = 0
    scope_known: bool = False
    scope_build: bool = False
    scope_upload: bool = False
    revision: int = 0
    dispatch_id: UUID | None = None
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    due_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    repair_after: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error_code: str | None = Field(default=None, max_length=64)
    failure_count: int = 0


class CapabilityRequest(SQLModel, table=True):
    __tablename__ = "account_capability_request"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "job_id"],
            [
                "account_capability_job.tenant_id",
                "account_capability_job.bc_id",
                "account_capability_job.id",
            ],
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    request_id: UUID = Field(primary_key=True)
    job_id: UUID


class CapabilityPage(SQLModel, table=True):
    __tablename__ = "account_capability_page"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "job_id"],
            [
                "account_capability_job.tenant_id",
                "account_capability_job.bc_id",
                "account_capability_job.id",
            ],
        ),
        CheckConstraint(
            "page > 0 AND row_count BETWEEN 0 AND 50", name="ck_capability_page"
        ),
    )
    job_id: UUID = Field(primary_key=True)
    page: int = Field(primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    row_count: int
    observed_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class CapabilityAsset(SQLModel, table=True):
    __tablename__ = "account_capability_asset"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "job_id"],
            [
                "account_capability_job.tenant_id",
                "account_capability_job.bc_id",
                "account_capability_job.id",
            ],
        ),
        ForeignKeyConstraint(
            ["job_id", "page"],
            ["account_capability_page.job_id", "account_capability_page.page"],
        ),
        CheckConstraint(
            "role IN ('ADMIN','OPERATOR','ANALYST')", name="ck_capability_asset_role"
        ),
    )
    job_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    page: int
    role: str = Field(max_length=16)
