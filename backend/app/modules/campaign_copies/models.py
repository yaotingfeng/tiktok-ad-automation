"""复制草稿与派发索引；源快照、覆盖意图、子预览保持独立。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def now() -> datetime:
    return datetime.now(UTC)


class CampaignCopyDraft(SQLModel, table=True):
    __tablename__ = "campaign_copy_draft"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_campaign_copy_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        CheckConstraint(
            "revision > 0 AND generation >= 0", name="ck_campaign_copy_revision"
        ),
        CheckConstraint(
            "status IN ('DRAFT','PREPARING','READY','SUBMITTED','FAILED')",
            name="ck_campaign_copy_status",
        ),
        Index("ix_campaign_copy_repair", "status", "repair_after", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    actor_id: UUID = Field(foreign_key="user.id")
    revision: int = 1
    status: str = "DRAFT"
    route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    rows: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    children: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    preview_digest: str | None = Field(default=None, max_length=64)
    error_code: str | None = Field(default=None, max_length=128)
    generation: int = 0
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    repair_after: datetime = Field(
        default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    created_at: datetime = Field(
        default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class CampaignCopyRequest(SQLModel, table=True):
    __tablename__ = "campaign_copy_request"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id"],
            ["campaign_copy_draft.tenant_id", "campaign_copy_draft.id"],
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    request_id: UUID = Field(primary_key=True)
    draft_id: UUID
    digest: str = Field(max_length=64)
    action: str = Field(max_length=16)
