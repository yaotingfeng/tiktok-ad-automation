"""当前草稿统一创意单元及有序成员事实。"""

from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.builds.image_composition_schemas import MediaKind, MusicStatus


class DraftCreativeUnit(SQLModel, table=True):
    __tablename__ = "draft_creative_unit"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id", "drama_id"],
            [
                "draft_drama.tenant_id",
                "draft_drama.draft_id",
                "draft_drama.bc_id",
                "draft_drama.drama_id",
            ],
            name="fk_creative_unit_drama",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id", "kind"],
            [
                "build_draft.tenant_id",
                "build_draft.id",
                "build_draft.bc_id",
                "build_draft.media_kind",
            ],
            name="fk_creative_unit_kind",
        ),
        UniqueConstraint(
            "tenant_id",
            "draft_id",
            "bc_id",
            "drama_id",
            "generation",
            "id",
            "kind",
            name="uq_creative_unit_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "draft_id",
            "drama_id",
            "generation",
            "position",
            name="uq_creative_unit_position",
        ),
        CheckConstraint("kind IN ('VIDEO','CAROUSEL')", name="ck_creative_unit_kind"),
        CheckConstraint(
            "generation > 0 AND position > 0 AND revision > 0 AND music_revision >= 0 AND music_request_generation >= 0",
            name="ck_creative_unit_versions",
        ),
        CheckConstraint(
            "music_status IN ('NOT_REQUIRED','NEEDS_SELECTION','PENDING','RUNNING','READY','FAILED')",
            name="ck_creative_unit_music_status",
        ),
        CheckConstraint(
            "(kind = 'VIDEO' AND music_status = 'NOT_REQUIRED' AND music_id IS NULL) OR (kind = 'CAROUSEL' AND music_status != 'NOT_REQUIRED')",
            name="ck_creative_unit_music_kind",
        ),
        CheckConstraint(
            "music_status != 'READY' OR (music_id IS NOT NULL AND length(music_id) > 0)",
            name="ck_creative_unit_ready_music",
        ),
        Index(
            "ix_creative_unit_music_progress",
            "tenant_id",
            "draft_id",
            "generation",
            "music_status",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    draft_id: UUID = Field(primary_key=True)
    generation: int = Field(primary_key=True)
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    bc_id: str = Field(max_length=128)
    drama_id: UUID
    kind: MediaKind = Field(
        default="VIDEO",
        sa_column=Column(String(16), nullable=False, server_default="VIDEO"),
    )
    position: int
    content_digest: str = Field(max_length=64)
    revision: int = 1
    composition_protected: bool = False
    music_protected: bool = False
    music_id: str | None = Field(default=None, max_length=255)
    music_rule_digest: str = Field(default="", max_length=64)
    music_status: MusicStatus = Field(
        default="NOT_REQUIRED", sa_column=Column(String(32), nullable=False)
    )
    music_revision: int = 0
    music_request_generation: int = 0
    # 展示快照不承载试听 URL 身份；T3 写回必须核对内容/规则/请求代数。
    music_snapshot: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )

    @property
    def unit_id(self) -> UUID:
        return self.id


class DraftCreativeUnitItem(SQLModel, table=True):
    __tablename__ = "draft_creative_unit_item"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "tenant_id",
                "draft_id",
                "bc_id",
                "drama_id",
                "generation",
                "unit_id",
                "kind",
            ],
            [
                "draft_creative_unit.tenant_id",
                "draft_creative_unit.draft_id",
                "draft_creative_unit.bc_id",
                "draft_creative_unit.drama_id",
                "draft_creative_unit.generation",
                "draft_creative_unit.id",
                "draft_creative_unit.kind",
            ],
            name="fk_creative_item_unit",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "material_id", "media_type"],
            ["material_file.tenant_id", "material_file.id", "material_file.media_type"],
            name="fk_creative_item_material",
        ),
        UniqueConstraint(
            "tenant_id",
            "draft_id",
            "generation",
            "unit_id",
            "material_id",
            name="uq_creative_item_material",
        ),
        CheckConstraint(
            "position BETWEEN 1 AND 35 AND (kind != 'VIDEO' OR position = 1)",
            name="ck_creative_item_position",
        ),
        CheckConstraint(
            "(kind = 'VIDEO' AND media_type = 'VIDEO') OR (kind = 'CAROUSEL' AND media_type = 'IMAGE')",
            name="ck_creative_item_media_type",
        ),
        Index("ix_creative_item_material", "tenant_id", "material_id"),
    )
    tenant_id: UUID = Field(primary_key=True)
    draft_id: UUID = Field(primary_key=True)
    generation: int = Field(primary_key=True)
    unit_id: UUID = Field(primary_key=True)
    position: int = Field(primary_key=True)
    bc_id: str = Field(max_length=128)
    drama_id: UUID
    material_id: UUID
    kind: MediaKind = Field(
        default="VIDEO",
        sa_column=Column(String(16), nullable=False, server_default="VIDEO"),
    )
    media_type: Literal["VIDEO", "IMAGE"] = Field(
        default="VIDEO",
        sa_column=Column(String(16), nullable=False, server_default="VIDEO"),
    )
