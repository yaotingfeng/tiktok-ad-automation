"""Bounded recovery jobs and immutable request receipts; no execution intent edits."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel

RecoveryKind = Literal["RETRY", "RECONCILE"]
RecoveryState = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class RecoveryRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class RecoveryReceipt(BaseModel):
    recovery_id: UUID
    request_id: UUID
    submission_id: UUID
    kind: RecoveryKind
    state: RecoveryState
    scheduled_count: int = Field(ge=0)
    reason_code: str | None = None


class HistoricalReadReceipt(BaseModel):
    read_id: UUID
    request_id: UUID
    source_step_id: UUID
    state: Literal["PENDING", "RUNNING", "CONFIRMED", "UNKNOWN", "BLOCKED"]
    remote_id: str | None = None
    mismatch: bool = False
    reason_code: str | None = None
    requires_new_preparation: Literal[True] = True


class SubmissionRecovery(SQLModel, table=True):
    __tablename__ = "submission_recovery"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_recovery_request"),
        UniqueConstraint(
            "tenant_id",
            "id",
            "request_id",
            "submission_id",
            "bc_id",
            "kind",
            name="uq_recovery_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "submission_id", "preview_id", "bc_id"],
            [
                "build_submission.tenant_id",
                "build_submission.id",
                "build_submission.preview_id",
                "build_submission.bc_id",
            ],
        ),
        CheckConstraint("kind IN ('RETRY','RECONCILE')", name="ck_recovery_kind"),
        CheckConstraint(
            "state IN ('QUEUED','RUNNING','COMPLETED','FAILED')",
            name="ck_recovery_state",
        ),
        CheckConstraint(
            "scheduled_count >= 0 AND scanned_count >= scheduled_count AND dispatch_revision >= 0",
            name="ck_recovery_counts",
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_recovery_lease",
        ),
        Index("ix_recovery_repair", "state", "repair_after", "id"),
        Index(
            "ix_recovery_submission", "tenant_id", "submission_id", "created_at", "id"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    request_id: UUID
    submission_id: UUID
    preview_id: UUID
    bc_id: str = Field(max_length=128)
    kind: str = Field(max_length=16)
    request_actor_id: UUID = Field(foreign_key="user.id")
    state: str = Field(default="QUEUED", max_length=16)
    cursor_step_id: UUID | None = None
    scanned_count: int = 0
    scheduled_count: int = 0
    reason_code: str | None = Field(default=None, max_length=128)
    lease_token: UUID | None = None
    lease_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    dispatch_revision: int = 0
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
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
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class SubmissionRecoveryRequest(SQLModel, table=True):
    """The immutable original QUEUED/0 receipt, never the mutable job progress."""

    __tablename__ = "submission_recovery_request"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "tenant_id",
                "recovery_id",
                "request_id",
                "submission_id",
                "bc_id",
                "kind",
            ],
            [
                "submission_recovery.tenant_id",
                "submission_recovery.id",
                "submission_recovery.request_id",
                "submission_recovery.submission_id",
                "submission_recovery.bc_id",
                "submission_recovery.kind",
            ],
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    request_id: UUID = Field(primary_key=True)
    recovery_id: UUID
    submission_id: UUID
    bc_id: str = Field(max_length=128)
    kind: str = Field(max_length=16)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
