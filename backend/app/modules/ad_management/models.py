"""广告管理预览、任务及外部请求回执的不可变持久化副本。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# 导入冻结选择模型，确保普通应用进程也登记复合外键目标。
from app.modules.reporting import query_models as _query_models  # noqa: F401


def utcnow() -> datetime:
    return datetime.now(UTC)


class ManagementPreview(SQLModel, table=True):
    __tablename__ = "management_preview"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_management_preview_tenant_id"),
        UniqueConstraint(
            "tenant_id", "bc_id", "id", name="uq_management_preview_scope_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "selection_id"],
            [
                "frozen_selection.tenant_id",
                "frozen_selection.bc_id",
                "frozen_selection.id",
            ],
            name="fk_management_preview_selection",
        ),
        CheckConstraint("expires_at > created_at", name="ck_management_preview_expiry"),
        CheckConstraint(
            "status IN ('PREPARING','READY','EXPIRED','OBSOLETE')",
            name="ck_management_preview_status",
        ),
        Index(
            "ix_management_preview_owner_expiry",
            "tenant_id",
            "bc_id",
            "actor_id",
            "expires_at",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    bc_id: str = Field(max_length=128)
    actor_id: UUID = Field(foreign_key="user.id")
    selection_id: UUID
    digest: str = Field(max_length=64)
    mutation: dict = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    route: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    expires_at: datetime = Field(
        default_factory=lambda: utcnow() + timedelta(minutes=5),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    status: str = Field(default="READY", max_length=16)
    counts: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))


class ManagementPreviewItem(SQLModel, table=True):
    __tablename__ = "management_preview_item"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "id", name="uq_management_preview_item_tenant_id"
        ),
        UniqueConstraint(
            "tenant_id", "preview_id", "id", name="uq_management_preview_item_scope"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "preview_id"],
            ["management_preview.tenant_id", "management_preview.id"],
            ondelete="CASCADE",
            name="fk_management_preview_item_preview",
        ),
        CheckConstraint(
            "execution_result IN ('PENDING','ACCEPTED','REJECTED','NOT_SENT','UNKNOWN','NO_CHANGE','CONFLICT','UNSUPPORTED')",
            name="ck_management_preview_item_result",
        ),
        Index(
            "ix_management_preview_item_order", "tenant_id", "preview_id", "position"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    preview_id: UUID
    position: int = Field(default=0, sa_column=Column(Integer, nullable=False))
    ref: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    material_use: dict | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    parent_ref: dict | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    grouping_revision: int = 0
    membership_digest: str | None = Field(default=None, max_length=64)
    capability: dict = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    original_value: str | None = Field(default=None, max_length=128)
    final_value: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=255)
    execution_result: str = Field(default="PENDING", max_length=16)
    observation_state: str | None = Field(default=None, max_length=32)
    delivery_status: str | None = Field(default=None, max_length=32)
    request_attribution: str | None = Field(default=None, max_length=64)


class ManagementTask(SQLModel, table=True):
    __tablename__ = "management_task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_management_task_tenant_id"),
        UniqueConstraint(
            "tenant_id", "id", "preview_id", name="uq_management_task_preview_scope"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "preview_id"],
            [
                "management_preview.tenant_id",
                "management_preview.bc_id",
                "management_preview.id",
            ],
            name="fk_management_task_preview",
        ),
        UniqueConstraint(
            "tenant_id",
            "actor_id",
            "idempotency_key",
            name="uq_management_task_idempotency",
        ),
        CheckConstraint(
            "status IN ('PREPARING','READY','QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','NEEDS_REVIEW','CANCELLED')",
            name="ck_management_task_status",
        ),
        Index(
            "ix_management_task_scope_status",
            "tenant_id",
            "bc_id",
            "status",
            "created_at",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    bc_id: str = Field(max_length=128)
    actor_id: UUID = Field(foreign_key="user.id")
    preview_id: UUID
    idempotency_key: UUID
    digest: str = Field(max_length=64)
    route: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    status: str = Field(default="QUEUED", max_length=16)
    counts: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ManagementTaskItem(SQLModel, table=True):
    __tablename__ = "management_task_item"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_management_task_item_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["management_task.tenant_id", "management_task.id"],
            ondelete="CASCADE",
            name="fk_management_task_item_task",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_id", "preview_id"],
            [
                "management_task.tenant_id",
                "management_task.id",
                "management_task.preview_id",
            ],
            name="fk_management_task_item_task_preview",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "preview_item_id"],
            ["management_preview_item.tenant_id", "management_preview_item.id"],
            name="fk_management_task_item_preview_item",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "preview_id", "preview_item_id"],
            [
                "management_preview_item.tenant_id",
                "management_preview_item.preview_id",
                "management_preview_item.id",
            ],
            name="fk_management_task_item_preview_scope",
        ),
        CheckConstraint(
            "execution_result IN ('PENDING','ACCEPTED','REJECTED','NOT_SENT','UNKNOWN','NO_CHANGE','CONFLICT','UNSUPPORTED')",
            name="ck_management_task_item_result",
        ),
        Index(
            "ix_management_task_item_status", "tenant_id", "task_id", "execution_result"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    task_id: UUID
    preview_item_id: UUID
    preview_id: UUID
    position: int = 0
    ref: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    material_use: dict | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    original_value: str | None = Field(default=None, max_length=128)
    final_value: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=255)
    membership_digest: str | None = Field(default=None, max_length=64)
    execution_result: str = Field(default="PENDING", max_length=16)
    observation_state: str | None = Field(default=None, max_length=32)
    delivery_status: str | None = Field(default=None, max_length=32)
    request_attribution: str | None = Field(default=None, max_length=64)
    grouping_revision: int = 0
    parent_ref: dict | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    capability: dict = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )


class ManagementRequestAttempt(SQLModel, table=True):
    __tablename__ = "management_request_attempt"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "id", name="uq_management_request_attempt_tenant_id"
        ),
        UniqueConstraint(
            "tenant_id",
            "task_item_id",
            "attempt",
            name="uq_management_request_attempt_number",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_item_id"],
            ["management_task_item.tenant_id", "management_task_item.id"],
            ondelete="CASCADE",
            name="fk_management_request_attempt_item",
        ),
        CheckConstraint(
            "outcome IN ('ACCEPTED','REJECTED','NOT_SENT','UNKNOWN')",
            name="ck_management_request_attempt_outcome",
        ),
        CheckConstraint("attempt >= 1", name="ck_management_request_attempt_number"),
        Index(
            "ix_management_request_attempt_item", "tenant_id", "task_item_id", "attempt"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    task_item_id: UUID
    attempt: int = 1
    request_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    outcome: str = Field(default="NOT_SENT", max_length=16)
    request_id: str | None = Field(default=None, max_length=255)
    payload: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    response: dict = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    retryable: bool = False


class ManagementReceipt(SQLModel, table=True):
    __tablename__ = "management_receipt"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "attempt_id"],
            ["management_request_attempt.tenant_id", "management_request_attempt.id"],
            ondelete="CASCADE",
            name="fk_management_receipt_attempt",
        ),
        CheckConstraint(
            "outcome IN ('ACCEPTED','REJECTED','NOT_SENT','UNKNOWN')",
            name="ck_management_receipt_outcome",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    attempt_id: UUID
    outcome: str = Field(max_length=16)
    request_id: str | None = Field(default=None, max_length=255)
    observed_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    response: dict = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
