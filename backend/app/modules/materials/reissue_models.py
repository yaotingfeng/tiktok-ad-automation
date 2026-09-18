"""一次明确授权只消费一个旧素材身份；历史授权和替代关系不可变。"""

from datetime import UTC, datetime
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


class MaterialReissueAuthorization(SQLModel, table=True):
    __tablename__ = "material_reissue_authorization"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_material_reissue_request"),
        *(
            UniqueConstraint(column, name=f"uq_material_reissue_{suffix}")
            for column, suffix in (
                ("old_distribution_id", "old_distribution"),
                ("new_distribution_id", "new_distribution"),
                ("old_cover_job_id", "old_cover"),
                ("new_cover_job_id", "new_cover"),
            )
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "submission_id"],
            ["build_submission.tenant_id", "build_submission.id"],
        ),
        *(
            ForeignKeyConstraint(
                ["tenant_id", column],
                [f"{table}.tenant_id", f"{table}.id"],
                name=f"fk_material_reissue_{suffix}",
            )
            for column, table, suffix in (
                ("old_distribution_id", "material_distribution", "old_distribution"),
                ("new_distribution_id", "material_distribution", "new_distribution"),
                ("old_cover_job_id", "material_cover_job", "old_cover"),
                ("new_cover_job_id", "material_cover_job", "new_cover"),
            )
        ),
        CheckConstraint(
            "(kind = 'VIDEO' AND old_distribution_id IS NOT NULL AND new_distribution_id IS NOT NULL "
            "AND old_distribution_id != new_distribution_id AND old_cover_job_id IS NULL AND new_cover_job_id IS NULL) OR "
            "(kind = 'COVER' AND old_cover_job_id IS NOT NULL AND new_cover_job_id IS NOT NULL "
            "AND old_cover_job_id != new_cover_job_id AND old_distribution_id IS NULL AND new_distribution_id IS NULL)",
            name="ck_material_reissue_kind",
        ),
        CheckConstraint(
            "accepted_duplicate_materials", name="ck_material_reissue_accepted"
        ),
        CheckConstraint(
            "scope_digest ~ '^[0-9a-f]{64}$'", name="ck_material_reissue_digest"
        ),
        CheckConstraint(
            "jsonb_typeof(details) = 'object'", name="ck_material_reissue_details"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    submission_id: UUID
    request_id: UUID
    actor_id: UUID = Field(foreign_key="user.id")
    kind: str = Field(max_length=16)
    old_distribution_id: UUID | None = None
    new_distribution_id: UUID | None = None
    old_cover_job_id: UUID | None = None
    new_cover_job_id: UUID | None = None
    scope_digest: str = Field(max_length=64)
    details: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    accepted_duplicate_materials: bool
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
