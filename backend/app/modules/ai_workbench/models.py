"""AI optimizer identity, immutable configuration versions and scoped bindings."""

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


def _now() -> datetime:
    return datetime.now(UTC)


class AIOptimizer(SQLModel, table=True):
    __tablename__ = "ai_optimizer"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ai_optimizer_tenant_id"),
        Index("ix_ai_optimizer_tenant", "tenant_id", "created_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    name: str = Field(max_length=120)
    active: bool = True
    latest_version: int = Field(default=0, ge=0)
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=_now, sa_type=DateTime(timezone=True))


class AIOptimizerStrategyBinding(SQLModel, table=True):
    """Current and historical strategy references for one optimizer.

    A partial unique index allows history to remain queryable while enforcing one
    active binding. The tenant columns in both foreign keys prevent cross-tenant
    IDs from being attached accidentally.
    """

    __tablename__ = "ai_optimizer_strategy_binding"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_binding_optimizer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "strategy_version_id"],
            ["strategy_version.tenant_id", "strategy_version.id"],
            name="fk_ai_binding_strategy_version",
        ),
        Index(
            "uq_ai_binding_one_active",
            "tenant_id",
            "optimizer_id",
            unique=True,
            postgresql_where="active IS TRUE",
        ),
        Index("ix_ai_binding_optimizer", "tenant_id", "optimizer_id", "created_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    optimizer_id: UUID
    strategy_version_id: UUID
    active: bool = True
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=_now, sa_type=DateTime(timezone=True))


class AIOptimizerVersion(SQLModel, table=True):
    """Immutable optimizer configuration snapshot used by downstream work."""

    __tablename__ = "ai_optimizer_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_ai_optimizer_version_tenant_id"),
        UniqueConstraint(
            "tenant_id", "optimizer_id", "number", name="uq_ai_optimizer_version_number"
        ),
        UniqueConstraint("tenant_id", "request_id", name="uq_ai_optimizer_version_request"),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_version_optimizer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "strategy_binding_id"],
            ["ai_optimizer_strategy_binding.tenant_id", "ai_optimizer_strategy_binding.id"],
            name="fk_ai_version_binding",
        ),
        CheckConstraint(
            "number > 0 AND daily_spend > 0 AND (target_roas IS NULL OR target_roas > 0)",
            name="ck_ai_optimizer_version_values",
        ),
        CheckConstraint(
            "priority IN ('SPEND','ROAS','BALANCED')", name="ck_ai_optimizer_priority"
        ),
        CheckConstraint(
            "optimization_mode IN ('AUTO','MANUAL')", name="ck_ai_optimizer_mode"
        ),
        Index("ix_ai_optimizer_version_optimizer", "tenant_id", "optimizer_id", "number"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    optimizer_id: UUID
    number: int
    daily_spend: Decimal = Field(sa_column=Column(Numeric(38, 12), nullable=False))
    target_roas: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(38, 12), nullable=True)
    )
    priority: str = Field(default="BALANCED", max_length=16)
    bc_id: str = Field(max_length=128)
    advertiser_ids: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    optimization_mode: str = Field(default="MANUAL", max_length=16)
    authorization_limits: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    strategy_binding_id: UUID
    request_id: UUID
    request_digest: str = Field(max_length=64)
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=_now, sa_type=DateTime(timezone=True))


class AIOptimizerRuleBinding(SQLModel, table=True):
    """Explicit optimizer-to-rule links; rules remain owned by automation_rules."""

    __tablename__ = "ai_optimizer_rule_binding"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "optimizer_id", "rule_id", name="uq_ai_optimizer_rule"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_rule_binding_optimizer",
        ),
        Index("ix_ai_rule_binding_optimizer", "tenant_id", "optimizer_id", "active"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    optimizer_id: UUID
    rule_id: UUID = Field(foreign_key="automation_rule.id")
    active: bool = True
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=_now, sa_type=DateTime(timezone=True))
