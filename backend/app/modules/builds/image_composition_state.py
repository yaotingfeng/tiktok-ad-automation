"""图片试算与恢复事实；草稿行锁串行化所有用户修改及 generation 切换。"""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def scope_fk():
    return ForeignKeyConstraint(
        ["tenant_id", "draft_id", "bc_id"],
        ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
    )


def snapshot_fk(name):
    return ForeignKeyConstraint(
        ["tenant_id", "draft_id", "bc_id", "snapshot_id"],
        [
            "image_candidate_snapshot.tenant_id",
            "image_candidate_snapshot.draft_id",
            "image_candidate_snapshot.bc_id",
            "image_candidate_snapshot.id",
        ],
        name=name,
    )


def json_field():
    return Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))


class ImageCompositionState(SQLModel, table=True):
    __tablename__ = "image_composition_state"
    __table_args__ = (
        scope_fk(),
        snapshot_fk("fk_image_state_snapshot"),
        CheckConstraint("next_generation > 0", name="ck_image_state_generation"),
    )
    tenant_id: UUID = Field(primary_key=True)
    draft_id: UUID = Field(primary_key=True)
    bc_id: str
    snapshot_id: UUID | None = None
    rule: dict[str, Any] = json_field()
    drama_patches: dict[str, Any] = json_field()
    next_generation: int = 1


class ImageCandidateSnapshot(SQLModel, table=True):
    __tablename__ = "image_candidate_snapshot"
    __table_args__ = (
        scope_fk(),
        UniqueConstraint(
            "tenant_id", "draft_id", "bc_id", "id", name="uq_image_snapshot_scope"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    draft_id: UUID
    bc_id: str
    base_revision: int
    # 内容一经生成不可漂移；候选启禁另存于 exclusions。
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ImageCandidateItem(SQLModel, table=True):
    __tablename__ = "image_candidate_item"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id", "snapshot_id"],
            [
                "image_candidate_snapshot.tenant_id",
                "image_candidate_snapshot.draft_id",
                "image_candidate_snapshot.bc_id",
                "image_candidate_snapshot.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id", "drama_id"],
            [
                "draft_drama.tenant_id",
                "draft_drama.draft_id",
                "draft_drama.bc_id",
                "draft_drama.drama_id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "material_id", "media_type"],
            ["material_file.tenant_id", "material_file.id", "material_file.media_type"],
        ),
        CheckConstraint("media_type = 'IMAGE'", name="ck_image_candidate_type"),
    )
    tenant_id: UUID = Field(primary_key=True)
    snapshot_id: UUID = Field(primary_key=True)
    drama_id: UUID = Field(primary_key=True)
    material_id: UUID = Field(primary_key=True)
    draft_id: UUID
    bc_id: str
    media_type: str = "IMAGE"
    candidate: dict[str, Any] = json_field()
    source: dict[str, Any] = json_field()


class ImageCandidateExclusion(SQLModel, table=True):
    __tablename__ = "image_candidate_exclusion"
    __table_args__ = (
        scope_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id", "drama_id"],
            [
                "draft_drama.tenant_id",
                "draft_drama.draft_id",
                "draft_drama.bc_id",
                "draft_drama.drama_id",
            ],
            name="fk_image_exclusion_drama",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    draft_id: UUID = Field(primary_key=True)
    drama_id: UUID = Field(primary_key=True)
    material_id: UUID = Field(primary_key=True)
    bc_id: str
    excluded: bool = False
    # 显式跨剧复用只授权当前 material/drama；新命中剧目仍须解决。
    cross_drama_allowed: bool = False


class ImageCompositionPlan(SQLModel, table=True):
    __tablename__ = "image_composition_plan"
    __table_args__ = (
        scope_fk(),
        snapshot_fk("fk_image_plan_snapshot"),
        UniqueConstraint(
            "tenant_id", "draft_id", "bc_id", "id", name="uq_image_plan_scope"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    draft_id: UUID
    bc_id: str
    snapshot_id: UUID
    base_revision: int
    input_digest: str
    route: dict[str, Any] = json_field()
    summary: dict[str, Any] = json_field()


class ImageCompositionPlanUnit(SQLModel, table=True):
    __tablename__ = "image_composition_plan_unit"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id", "plan_id"],
            [
                "image_composition_plan.tenant_id",
                "image_composition_plan.draft_id",
                "image_composition_plan.bc_id",
                "image_composition_plan.id",
            ],
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    plan_id: UUID = Field(primary_key=True)
    ordinal: int = Field(primary_key=True)
    draft_id: UUID
    bc_id: str
    drama_id: UUID
    unit: dict[str, Any] = json_field()


class ImageCompositionRun(SQLModel, table=True):
    __tablename__ = "image_composition_run"
    __table_args__ = (
        CheckConstraint(
            "generation > 0 AND base_revision > 0 AND total > 0 AND cursor >= 0 AND cursor <= total",
            name="ck_image_run_progress",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id", "plan_id"],
            [
                "image_composition_plan.tenant_id",
                "image_composition_plan.draft_id",
                "image_composition_plan.bc_id",
                "image_composition_plan.id",
            ],
        ),
        UniqueConstraint(
            "tenant_id", "draft_id", "generation", name="uq_image_run_generation"
        ),
        Index(
            "uq_image_run_pending",
            "tenant_id",
            "draft_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_image_run_recovery", "status", "repair_after"),
        CheckConstraint(
            "status IN ('PENDING','APPLIED','CANCELLED','FAILED')",
            name="ck_image_run_status",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    draft_id: UUID
    bc_id: str
    actor_id: UUID
    plan_id: UUID
    generation: int
    base_revision: int
    status: str = "PENDING"
    cursor: int = 0
    total: int
    error_code: str | None = None
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    repair_after: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ImageCompositionRequest(SQLModel, table=True):
    __tablename__ = "image_composition_request"
    __table_args__ = (scope_fk(),)
    tenant_id: UUID = Field(primary_key=True)
    request_id: UUID = Field(primary_key=True)
    draft_id: UUID
    bc_id: str
    digest: str
    result: dict[str, Any] = json_field()
