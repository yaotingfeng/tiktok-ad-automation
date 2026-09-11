"""One permanent image-upload identity per actual target video and connection."""

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

from .models import access_reference, material_reference
from .routes import route_constraint


def utcnow() -> datetime:
    return datetime.now(UTC)


class MaterialCoverJob(SQLModel, table=True):
    __tablename__ = "material_cover_job"
    __table_args__ = (
        route_constraint("material_cover_job", "frozen_route", connection=True),
        CheckConstraint(
            "video_md5 IS NULL OR video_md5 ~ '^[0-9a-f]{32}$'",
            name="ck_material_cover_video_md5",
        ),
        material_reference(),
        access_reference(),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id", "asset_id"],
            [
                "account_material.tenant_id",
                "account_material.bc_id",
                "account_material.material_id",
                "account_material.id",
            ],
        ),
        UniqueConstraint("tenant_id", "id", name="uq_material_cover_scope"),
        UniqueConstraint(
            "tenant_id",
            "asset_id",
            "connection_id",
            "video_id",
            name="uq_material_cover_video",
        ),
        CheckConstraint(
            "status IN ('PENDING','PREPARING','VERIFYING','READY','UNKNOWN','BLOCKED')",
            name="ck_material_cover_status",
        ),
        CheckConstraint(
            "revision >= 0 AND next_page > 0 AND failure_count >= 0",
            name="ck_material_cover_counters",
        ),
        CheckConstraint(
            "(claim_token IS NULL) = (claimed_until IS NULL)",
            name="ck_material_cover_claim",
        ),
        CheckConstraint(
            "length(trim(video_id)) > 0 AND length(trim(remote_name)) > 0",
            name="ck_material_cover_identity",
        ),
        CheckConstraint(
            "known_image_id IS NULL OR request_armed_at IS NOT NULL",
            name="ck_material_cover_receipt",
        ),
        Index("ix_material_cover_repair", "status", "repair_after", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    material_id: UUID
    asset_id: UUID
    advertiser_id: str = Field(max_length=128)
    connection_id: UUID
    actor_id: UUID = Field(foreign_key="user.id")
    frozen_route: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    video_id: str = Field(max_length=255)
    video_md5: str | None = Field(default=None, max_length=32)
    remote_name: str = Field(max_length=128)
    status: str = Field(default="PENDING", max_length=16)
    request_armed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    known_image_id: str | None = Field(default=None, max_length=255)
    signature: str | None = Field(default=None, max_length=128)
    width: int | None = None
    height: int | None = None
    revision: int = 0
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    next_page: int = 1
    search_round: UUID = Field(default_factory=uuid4)
    search_total: int | None = None
    candidate_image_id: str | None = Field(default=None, max_length=255)
    search_ambiguous: bool = False
    error_code: str | None = Field(default=None, max_length=64)
    failure_count: int = 0
    repair_after: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class MaterialCoverReceipt(SQLModel, table=True):
    """Append-only known-ID fallback if the fenced state transition fails."""

    __tablename__ = "material_cover_receipt"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["material_cover_job.tenant_id", "material_cover_job.id"],
        ),
        UniqueConstraint("job_id", "image_id", name="uq_material_cover_receipt_image"),
        CheckConstraint(
            "receipt_facts IS NULL OR (jsonb_typeof(receipt_facts) = 'object' "
            "AND receipt_facts ? 'signature' "
            "AND receipt_facts - 'signature' = '{}'::jsonb "
            "AND (receipt_facts->'signature' = 'null'::jsonb OR "
            "(jsonb_typeof(receipt_facts->'signature') = 'string' "
            "AND receipt_facts->>'signature' ~ '^[0-9a-f]{32}$')))",
            name="ck_material_cover_receipt_facts",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    job_id: UUID
    image_id: str = Field(max_length=255)
    receipt_facts: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    observed_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class MaterialCoverJobPage(SQLModel, table=True):
    """At most one 100-ID search page per task; no raw SDK payloads or URLs."""

    __tablename__ = "material_cover_job_page"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["material_cover_job.tenant_id", "material_cover_job.id"],
        ),
        UniqueConstraint(
            "job_id", "search_round", "page", name="uq_material_cover_search_page"
        ),
        CheckConstraint(
            "page > 0 AND total >= 0 AND jsonb_array_length(image_ids) <= 100",
            name="ck_material_cover_page_size",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    job_id: UUID
    search_round: UUID
    page: int
    total: int
    image_ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
