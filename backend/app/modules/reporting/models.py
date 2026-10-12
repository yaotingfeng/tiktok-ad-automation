"""账户余额观测保持独立，不参与广告收入与消耗汇总。"""

from datetime import datetime
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import account_reference


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
