"""Frozen intent is independent of editable draft rows and remote execution state."""

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


def preview_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "preview_id", "bc_id"],
        ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
    )


class BuildPreview(SQLModel, table=True):
    __tablename__ = "build_preview"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", "bc_id", name="uq_preview_bc"),
        UniqueConstraint("tenant_id", "id", name="uq_preview_tenant"),
        UniqueConstraint(
            "tenant_id", "draft_id", "draft_revision", name="uq_preview_revision"
        ),
        UniqueConstraint("batch_short_id", name="uq_preview_batch_short"),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id"],
            ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "strategy_version_id"],
            ["strategy_version.tenant_id", "strategy_version.id"],
        ),
        CheckConstraint(
            "status IN ('BUILDING','FROZEN','OBSOLETE','FAILED')",
            name="ck_preview_status",
        ),
        CheckConstraint(
            "draft_revision > 0 AND generation >= 0", name="ck_preview_revision"
        ),
        Index("ix_preview_repair", "status", "repair_after", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    draft_id: UUID
    draft_revision: int
    strategy_version_id: UUID
    actor_id: UUID = Field(foreign_key="user.id")
    batch_short_id: str = Field(max_length=32)
    local_date: str = Field(max_length=8)
    status: str = "BUILDING"
    config: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    budget: Decimal = Field(sa_column=Column(Numeric(38, 12), nullable=False))
    target_roas: Decimal = Field(sa_column=Column(Numeric(38, 12), nullable=False))
    progress: dict[str, Any] = Field(
        default_factory=lambda: {"phase": "inputs", "after": 0},
        sa_column=Column(JSONB, nullable=False),
    )
    content_digest: str | None = Field(default=None, max_length=64)
    generation: int = 0
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    repair_after: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    error_code: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PreviewRow(SQLModel):
    tenant_id: UUID = Field(primary_key=True)
    preview_id: UUID = Field(primary_key=True)
    bc_id: str = Field(max_length=128)


class PreviewInput(PreviewRow, table=True):
    __tablename__ = "preview_input"
    __table_args__ = (
        preview_fk(),
        CheckConstraint(
            "kind IN ('account','drama') AND line_no > 0", name="ck_preview_input"
        ),
    )
    kind: str = Field(primary_key=True, max_length=16)
    line_no: int = Field(primary_key=True)
    raw_text: str = Field(max_length=1000)
    status: str = Field(max_length=32)
    reason_code: str | None = None
    duplicate_of: int | None = None


class PreviewDrama(PreviewRow, table=True):
    __tablename__ = "preview_drama"
    __table_args__ = (
        preview_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "drama_id"], ["provider_drama.tenant_id", "provider_drama.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "link_id"], ["promotion_link.tenant_id", "promotion_link.id"]
        ),
    )
    drama_id: UUID = Field(primary_key=True)
    link_id: UUID
    title: str = Field(max_length=1000)
    url: str
    protected_base: str
    reason_codes: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )


class PreviewDramaGroup(PreviewRow, table=True):
    __tablename__ = "preview_drama_group"
    __table_args__ = (
        preview_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "preview_id", "drama_id"],
            [
                "preview_drama.tenant_id",
                "preview_drama.preview_id",
                "preview_drama.drama_id",
            ],
        ),
        UniqueConstraint(
            "tenant_id", "preview_id", "id", name="uq_preview_drama_group_id"
        ),
        CheckConstraint("group_no > 0", name="ck_preview_group_no"),
    )
    drama_id: UUID = Field(primary_key=True)
    group_no: int = Field(primary_key=True)
    id: UUID = Field(default_factory=uuid4)


def drama_group_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "preview_id", "drama_id", "group_no"],
        [
            "preview_drama_group.tenant_id",
            "preview_drama_group.preview_id",
            "preview_drama_group.drama_id",
            "preview_drama_group.group_no",
        ],
    )


class PreviewGroupMaterial(PreviewRow, table=True):
    __tablename__ = "preview_group_material"
    __table_args__ = (
        preview_fk(),
        drama_group_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id"],
            ["material_file.tenant_id", "material_file.bc_id", "material_file.id"],
        ),
        UniqueConstraint(
            "tenant_id",
            "preview_id",
            "drama_id",
            "material_id",
            name="uq_preview_drama_material",
        ),
        CheckConstraint("position > 0", name="ck_preview_material_position"),
    )
    drama_id: UUID = Field(primary_key=True)
    group_no: int = Field(primary_key=True)
    position: int = Field(primary_key=True)
    material_id: UUID


class PreviewCopy(PreviewRow, table=True):
    __tablename__ = "preview_copy"
    __table_args__ = (
        preview_fk(),
        drama_group_fk(),
        CheckConstraint("creative_no > 0", name="ck_preview_copy_no"),
    )
    drama_id: UUID = Field(primary_key=True)
    group_no: int = Field(primary_key=True)
    creative_no: int = Field(primary_key=True)
    copy_id: UUID = Field(foreign_key="copy_entry.id")
    text: str


class BuildUnit(SQLModel, table=True):
    __tablename__ = "build_unit"
    __table_args__ = (
        preview_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "preview_id", "drama_id"],
            [
                "preview_drama.tenant_id",
                "preview_drama.preview_id",
                "preview_drama.drama_id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "advertiser_id", "connection_id"],
            [
                "bc_account_access.tenant_id",
                "bc_account_access.bc_id",
                "bc_account_access.advertiser_id",
                "bc_account_access.connection_id",
            ],
        ),
        UniqueConstraint(
            "tenant_id", "id", "preview_id", "bc_id", name="uq_build_unit_preview"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_build_unit_tenant"),
        UniqueConstraint(
            "tenant_id",
            "preview_id",
            "drama_id",
            "advertiser_id",
            name="uq_build_unit_pair",
        ),
        CheckConstraint(
            "readiness IN ('READY','PREPARING','BLOCKED')",
            name="ck_build_unit_readiness",
        ),
        Index(
            "ix_build_unit_names",
            "tenant_id",
            "preview_id",
            "advertiser_id",
            "campaign_digest",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    preview_id: UUID
    bc_id: str = Field(max_length=128)
    drama_id: UUID
    advertiser_id: str = Field(max_length=128)
    connection_id: UUID
    currency: str = Field(max_length=8)
    timezone: str = Field(max_length=128)
    campaign_name: str
    campaign_digest: str = Field(max_length=64)
    readiness: str = "READY"
    reason_codes: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    scene_snapshot: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    group_count: int = 0
    ad_count: int = 0
    complete: bool = False


class PlannedGroup(PreviewRow, table=True):
    __tablename__ = "planned_group"
    __table_args__ = (
        preview_fk(),
        drama_group_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "unit_id", "preview_id", "bc_id"],
            [
                "build_unit.tenant_id",
                "build_unit.id",
                "build_unit.preview_id",
                "build_unit.bc_id",
            ],
        ),
        UniqueConstraint(
            "tenant_id", "unit_id", "group_no", name="uq_planned_group_no"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    unit_id: UUID
    drama_id: UUID
    group_no: int
    name: str


class PlannedAd(PreviewRow, table=True):
    __tablename__ = "planned_ad"
    __table_args__ = (
        preview_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "preview_id", "group_id"],
            ["planned_group.tenant_id", "planned_group.preview_id", "planned_group.id"],
        ),
        UniqueConstraint(
            "tenant_id", "group_id", "creative_no", name="uq_planned_ad_no"
        ),
        CheckConstraint("creative_no > 0", name="ck_planned_ad_no"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    group_id: UUID
    creative_no: int
    name: str
    copy_id: UUID = Field(foreign_key="copy_entry.id")
    text: str
    cta_option_ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
