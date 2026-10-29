"""配乐事实与订阅栅栏；同批相同组合只产生一个远端推荐。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class MusicSelectionJob(SQLModel, table=True):
    __tablename__ = "music_selection_job"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id"],
            ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
        ),
        UniqueConstraint(
            "tenant_id",
            "draft_id",
            "active_generation",
            "fingerprint",
            name="uq_music_job_fingerprint",
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','READY','FAILED','CANCELLED')",
            name="ck_music_job_status",
        ),
        CheckConstraint(
            "active_generation > 0 AND generation > 0 AND attempts >= 0 AND cursor >= 0 AND cursor <= jsonb_array_length(targets)",
            name="ck_music_job_progress",
        ),
        Index("ix_music_job_repair", "status", "repair_after"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    draft_id: UUID
    bc_id: str
    actor_id: UUID = Field(foreign_key="user.id")
    active_generation: int
    generation: int = 1
    fingerprint: str = Field(max_length=64)
    route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    seed_advertiser_id: str | None = Field(default=None, max_length=128)
    parameters: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    material_ids: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    targets: list[dict[str, Any]] = Field(sa_column=Column(JSONB, nullable=False))
    result: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    status: str = "PENDING"
    attempts: int = 0
    wait_turns: int = 0
    cursor: int = 0
    error_code: str | None = None
    claim: UUID | None = None
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    repair_after: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class MusicPreparationRun(SQLModel, table=True):
    __tablename__ = "music_preparation_run"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id"],
            ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
        ),
        CheckConstraint(
            "status IN ('PENDING','READY','FAILED','CANCELLED')",
            name="ck_music_preparation_status",
        ),
        CheckConstraint(
            "active_generation > 0 AND generation > 0 AND processed >= 0",
            name="ck_music_preparation_progress",
        ),
        Index("ix_music_preparation_repair", "status", "repair_after"),
        CheckConstraint(
            "action IN ('PREPARE','RETRY_FAILED') AND (action != 'RETRY_FAILED' OR (base_revision IS NOT NULL AND base_revision > 0 AND jsonb_array_length(drama_ids) BETWEEN 1 AND 100)) AND retried >= 0 AND skipped_protected >= 0 AND skipped_recovered >= 0",
            name="ck_music_preparation_action",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    draft_id: UUID
    bc_id: str
    actor_id: UUID = Field(foreign_key="user.id")
    active_generation: int
    generation: int = 1
    route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    status: str = "PENDING"
    action: str = Field(
        default="PREPARE",
        sa_column=Column(String, nullable=False, server_default="PREPARE"),
    )
    drama_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    base_revision: int | None = None
    retried: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    skipped_protected: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    skipped_recovered: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    after_id: UUID | None = None
    processed: int = 0
    error_code: str | None = None
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    repair_after: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
