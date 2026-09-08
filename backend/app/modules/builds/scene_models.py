"""Bounded page claims and append-only sanitized scene evidence."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class SceneReadState(SQLModel, table=True):
    __tablename__ = "scene_read_state"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_scene_state_tenant"),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "advertiser_id",
            "link_id",
            "resource",
            name="uq_scene_state_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id"],
            ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "link_id"], ["promotion_link.tenant_id", "promotion_link.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        CheckConstraint(
            "resource IN ('account_roles','identity','minis','cta','vbo')",
            name="ck_scene_resource",
        ),
        CheckConstraint("next_page > 0", name="ck_scene_page"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    bc_id: str = Field(max_length=128)
    advertiser_id: str = Field(max_length=128)
    link_id: UUID
    connection_id: UUID
    resource: str = Field(max_length=32)
    basis_digest: str = Field(max_length=64)
    generation: UUID = Field(default_factory=uuid4)
    attempt_token: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    next_page: int = 1
    last_evidence_id: UUID | None = None
    complete: bool = False
    facts: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error_code: str | None = Field(default=None, max_length=64)


class SceneEvidence(SQLModel, table=True):
    __tablename__ = "scene_evidence"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_scene_evidence_tenant"),
        UniqueConstraint(
            "state_id", "generation", "page", name="uq_scene_evidence_page"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "state_id"],
            ["scene_read_state.tenant_id", "scene_read_state.id"],
        ),
        CheckConstraint("page > 0", name="ck_scene_evidence_page"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    state_id: UUID
    generation: UUID
    page: int
    endpoint: str = Field(max_length=255)
    request_id: str | None = Field(default=None, max_length=128)
    source_revision: str = Field(max_length=64)
    basis_digest: str = Field(max_length=64)
    facts: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    observed_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
