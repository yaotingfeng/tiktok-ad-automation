"""批次回执、外部业务身份和不可变素材版本分开持久化。"""

from datetime import UTC, datetime
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


class MaterialPushBatch(SQLModel, table=True):
    __tablename__ = "material_push_batch"
    __table_args__ = (
        UniqueConstraint("key_id", "request_id", name="uq_push_batch_request"),
        UniqueConstraint("tenant_id", "id", name="uq_push_batch_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["tenant_membership.tenant_id", "tenant_membership.user_id"],
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    tenant_name: str = Field(max_length=120)
    bc_id: str = Field(max_length=128)
    actor_id: UUID
    key_id: str = Field(max_length=128)
    request_id: UUID
    request_digest: str = Field(max_length=64)
    frozen_route: dict = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(
        default_factory=now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class PushedMaterial(SQLModel, table=True):
    __tablename__ = "pushed_material"
    __table_args__ = (
        CheckConstraint("latest_revision > 0", name="ck_pushed_revision"),
        ForeignKeyConstraint(
            ["tenant_id", "current_material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
    )
    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    external_id: str = Field(primary_key=True, max_length=255)
    latest_revision: int = 1
    current_material_id: UUID | None = None


class MaterialPushItem(SQLModel, table=True):
    __tablename__ = "material_push_item"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_push_item_scope"),
        UniqueConstraint(
            "tenant_id", "external_id", "revision", name="uq_push_item_revision"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            ["material_push_batch.tenant_id", "material_push_batch.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "external_id"],
            ["pushed_material.tenant_id", "pushed_material.external_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
        CheckConstraint(
            "status IN ('queued','validating','imported','failed')",
            name="ck_push_item_status",
        ),
        CheckConstraint("revision > 0 AND attempts >= 0", name="ck_push_item_numbers"),
        Index("ix_push_item_repair", "status", "claimed_until", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    batch_id: UUID
    external_id: str = Field(max_length=255)
    revision: int
    file_name: str = Field(max_length=100)
    url_ciphertext: str = Field(repr=False)
    material_id: UUID | None = None
    status: str = Field(default="queued", max_length=16)
    error_code: str | None = Field(default=None, max_length=128)
    attempts: int = 0
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")


class ExternalMaterialSource(SQLModel, table=True):
    __tablename__ = "external_material_source"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "item_id"],
            ["material_push_item.tenant_id", "material_push_item.id"],
        ),
    )
    material_id: UUID = Field(primary_key=True)
    tenant_id: UUID
    item_id: UUID
    etag: str | None = Field(default=None, max_length=512)
