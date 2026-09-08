from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Tenant(SQLModel, table=True):
    __tablename__ = "tenant"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=120)
    active: bool = True


class TenantMembership(SQLModel, table=True):
    __tablename__ = "tenant_membership"
    __table_args__ = (
        CheckConstraint(
            "role IN ('tenant_admin','operator','viewer')",
            name="ck_tenant_membership_role",
        ),
        Index("ix_tenant_membership_user_tenant", "user_id", "tenant_id"),
    )

    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    user_id: UUID = Field(foreign_key="user.id", primary_key=True)
    role: str = Field(max_length=32)
    active: bool = True


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_event"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    actor_id: UUID = Field(foreign_key="user.id")
    action: str = Field(max_length=120)
    target_id: str = Field(max_length=255)
    details: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
