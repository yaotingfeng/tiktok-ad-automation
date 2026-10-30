"""物理管理请求及其成员；一次平台请求与逐项审计分别持久化。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel


class ManagementPhysicalRequest(SQLModel, table=True):
    __tablename__ = "management_physical_request"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "id", name="uq_management_physical_scope"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["management_task.tenant_id", "management_task.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("member_count >= 1", name="ck_management_physical_size"),
        CheckConstraint(
            "status IN ('RECORDED','COMPLETE','UNKNOWN')",
            name="ck_management_physical_status",
        ),
        Index("ix_management_physical_task", "tenant_id", "task_id", "created_at"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    task_id: UUID
    endpoint: str = Field(max_length=255)
    member_count: int
    status: str = Field(default="RECORDED", max_length=16)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ManagementPhysicalMember(SQLModel, table=True):
    __tablename__ = "management_physical_member"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "task_id", "batch_id"],
            [
                "management_physical_request.tenant_id",
                "management_physical_request.task_id",
                "management_physical_request.id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_id", "task_item_id"],
            [
                "management_task_item.tenant_id",
                "management_task_item.task_id",
                "management_task_item.id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_item_id", "attempt_id"],
            [
                "management_request_attempt.tenant_id",
                "management_request_attempt.task_item_id",
                "management_request_attempt.id",
            ],
            ondelete="CASCADE",
        ),
        UniqueConstraint("attempt_id", name="uq_management_physical_attempt"),
        UniqueConstraint(
            "batch_id", "task_item_id", name="uq_management_physical_member"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    task_id: UUID
    batch_id: UUID
    task_item_id: UUID
    attempt_id: UUID
