from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class PendingDispatch(SQLModel, table=True):
    __tablename__ = "pending_dispatch"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_key", name="uq_dispatch_tenant_key"),
        Index(
            "ix_dispatch_pending",
            "available_at",
            "id",
            postgresql_where=text("published_at IS NULL"),
        ),
        Index(
            "ix_dispatch_tenant_pending",
            "tenant_id",
            "available_at",
            "id",
            postgresql_where=text("published_at IS NULL"),
        ),
        Index(
            "ix_dispatch_expansion_pending",
            "tenant_id",
            "available_at",
            "id",
            postgresql_where=text(
                "published_at IS NULL AND task_name = 'builds.expand_submission'"
            ),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    actor_id: UUID
    task_name: str = Field(max_length=255)
    task_key: str = Field(max_length=255)
    payload: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    available_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
        ),
    )
    published_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    attempts: int = Field(default=0)


class DispatchTenantCursor(SQLModel, table=True):
    __tablename__ = "dispatch_tenant_cursor"

    tenant_id: UUID = Field(primary_key=True)
    last_published_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
