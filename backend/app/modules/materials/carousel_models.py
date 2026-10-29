"""可复用轮播版本与有类型的图片引用；版本只能追加，不能原地修改。"""

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


class CarouselPresetVersion(SQLModel, table=True):
    __tablename__ = "carousel_preset_version"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "source_bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        UniqueConstraint("tenant_id", "id", name="uq_carousel_preset_scope"),
        UniqueConstraint(
            "tenant_id", "name", "version", name="uq_carousel_preset_version"
        ),
        UniqueConstraint(
            "tenant_id", "copy_request_id", name="uq_carousel_preset_copy_request"
        ),
        CheckConstraint(
            "(copy_request_id IS NULL AND copy_request_digest IS NULL) OR (copy_request_id IS NOT NULL AND copy_request_digest IS NOT NULL AND length(copy_request_digest) = 64)",
            name="ck_carousel_preset_copy_request",
        ),
        CheckConstraint(
            "version > 0 AND length(trim(name)) BETWEEN 1 AND 200",
            name="ck_carousel_preset_version",
        ),
        CheckConstraint(
            "jsonb_typeof(ordered_material_ids) = 'array' AND jsonb_array_length(ordered_material_ids) BETWEEN 1 AND 35",
            name="ck_carousel_preset_images",
        ),
        CheckConstraint(
            "music_status IN ('READY','PENDING','NEEDS_SELECTION') AND ((music_status = 'READY' AND music_id IS NOT NULL AND length(music_id) > 0) OR (music_status != 'READY' AND music_id IS NULL))",
            name="ck_carousel_preset_music",
        ),
        Index("ix_carousel_preset_name", "tenant_id", "name", "version"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    source_bc_id: str = Field(max_length=128)
    source_draft_id: UUID
    source_unit_id: UUID
    source_drama_id: UUID
    actor_id: UUID = Field(foreign_key="user.id")
    name: str = Field(max_length=200)
    version: int
    content_digest: str = Field(max_length=64)
    ordered_material_ids: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    music_id: str | None = Field(default=None, max_length=255)
    music_status: str = Field(max_length=32)
    music_snapshot: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    copy_request_id: UUID | None = None
    copy_request_digest: str | None = Field(default=None, max_length=64)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class CarouselPresetItem(SQLModel, table=True):
    __tablename__ = "carousel_preset_item"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["carousel_preset_version.tenant_id", "carousel_preset_version.id"],
            name="fk_carousel_preset_item_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "material_id", "media_type"],
            ["material_file.tenant_id", "material_file.id", "material_file.media_type"],
            name="fk_carousel_preset_item_material",
        ),
        UniqueConstraint(
            "tenant_id",
            "version_id",
            "material_id",
            name="uq_carousel_preset_item_material",
        ),
        CheckConstraint(
            "position BETWEEN 1 AND 35 AND media_type = 'IMAGE'",
            name="ck_carousel_preset_item_type",
        ),
        Index("ix_carousel_preset_material", "tenant_id", "material_id"),
    )
    tenant_id: UUID = Field(primary_key=True)
    version_id: UUID = Field(primary_key=True)
    position: int = Field(primary_key=True)
    material_id: UUID
    media_type: str = Field(default="IMAGE", max_length=16)
    content_digest: str = Field(max_length=64)
