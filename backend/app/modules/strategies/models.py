from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
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


class CopyPoolVersion(SQLModel, table=True):
    __tablename__ = "copy_pool_version"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=120)
    sealed: bool = False
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class CopyEntry(SQLModel, table=True):
    __tablename__ = "copy_entry"
    __table_args__ = (
        UniqueConstraint("pool_version_id", "position"),
        UniqueConstraint("pool_version_id", "text"),
        CheckConstraint("position > 0", name="ck_copy_position"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    pool_version_id: UUID = Field(foreign_key="copy_pool_version.id", index=True)
    position: int
    text: str
    enabled: bool = True


class Strategy(SQLModel, table=True):
    __tablename__ = "strategy"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_strategy_tenant_id"),
        CheckConstraint("latest_version >= 0", name="ck_strategy_version"),
        Index("ix_strategy_tenant_page", "tenant_id", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    name: str = Field(max_length=120)
    active: bool = True
    latest_version: int = 0


class StrategyVersion(SQLModel, table=True):
    __tablename__ = "strategy_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_strategy_version_tenant_id"),
        UniqueConstraint(
            "tenant_id", "strategy_id", "number", name="uq_strategy_version_number"
        ),
        UniqueConstraint("tenant_id", "request_id", name="uq_strategy_version_request"),
        ForeignKeyConstraint(
            ["tenant_id", "strategy_id"], ["strategy.tenant_id", "strategy.id"]
        ),
        CheckConstraint(
            "number > 0 AND budget > 0 AND target_roas > 0 AND budget != 'NaN'::numeric AND target_roas != 'NaN'::numeric",
            name="ck_strategy_version_values",
        ),
        CheckConstraint(
            "request_kind IN ('create','append')", name="ck_strategy_request_kind"
        ),
        CheckConstraint(
            "coalesce((jsonb_typeof(config) = 'object' AND (config->>'budget')::numeric = budget AND (config->>'target_roas')::numeric = target_roas AND config->>'copy_pool_version' = copy_pool_version_id::text), false)",
            name="ck_strategy_config_identity",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    strategy_id: UUID
    number: int
    copy_pool_version_id: UUID = Field(foreign_key="copy_pool_version.id")
    config: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    budget: Decimal = Field(sa_column=Column(Numeric(38, 12), nullable=False))
    target_roas: Decimal = Field(sa_column=Column(Numeric(38, 12), nullable=False))
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    request_id: UUID
    request_digest: str = Field(max_length=64)
    request_kind: str = Field(max_length=16)
