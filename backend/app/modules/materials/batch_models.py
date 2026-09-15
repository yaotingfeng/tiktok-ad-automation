"""共享批次保留原操作身份；一次物理请求对应不可覆盖的回执。"""

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

from .routes import route_constraint


class MaterialShareBatch(SQLModel, table=True):
    __tablename__ = "material_share_batch"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "bc_id", "id", name="uq_material_share_batch_scope"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        route_constraint("material_share_batch", "target_route"),
        route_constraint("material_share_batch", "source_route"),
        CheckConstraint(
            "status IN ('pending','sending','acknowledged','result_unknown','not_sent','failed','completed')",
            name="ck_material_share_batch_status",
        ),
        CheckConstraint(
            "jsonb_typeof(material_ids) = 'array' AND jsonb_array_length(material_ids) <= 20 AND (wire_digest IS NULL OR jsonb_array_length(material_ids) >= 1)",
            name="ck_material_share_batch_materials",
        ),
        CheckConstraint(
            "jsonb_typeof(advertiser_ids) = 'array' AND jsonb_array_length(advertiser_ids) BETWEEN 1 AND 10",
            name="ck_material_share_batch_targets",
        ),
        CheckConstraint(
            "length(request_digest) = 64 AND (wire_digest IS NULL OR length(wire_digest) = 64)",
            name="ck_material_share_batch_digests",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    actor_id: UUID = Field(foreign_key="user.id")
    source_advertiser_id: str = Field(max_length=128)
    target_route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    source_route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    request_digest: str = Field(max_length=64)
    wire_digest: str | None = Field(default=None, max_length=64)
    claim_id: UUID
    status: str = "pending"
    material_ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    advertiser_ids: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class MaterialShareBatchMember(SQLModel, table=True):
    __tablename__ = "material_share_batch_member"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "batch_id"],
            [
                "material_share_batch.tenant_id",
                "material_share_batch.bc_id",
                "material_share_batch.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id", "advertiser_id", "operation_id"],
            [
                "material_asset_operation.tenant_id",
                "material_asset_operation.bc_id",
                "material_asset_operation.material_id",
                "material_asset_operation.advertiser_id",
                "material_asset_operation.id",
            ],
        ),
        UniqueConstraint(
            "batch_id", "operation_id", name="uq_material_share_batch_operation"
        ),
        Index("ix_material_share_member_operation", "tenant_id", "operation_id"),
        CheckConstraint(
            "status IN ('pending','verifying','result_unknown','not_sent','failed','ready')",
            name="ck_material_share_member_status",
        ),
        CheckConstraint(
            "revision >= 0 AND length(operation_digest) = 64 AND jsonb_typeof(source_evidence) = 'object'",
            name="ck_material_share_member_evidence",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    batch_id: UUID
    material_id: UUID
    advertiser_id: str = Field(max_length=128)
    distribution_id: UUID = Field(foreign_key="material_distribution.id")
    operation_id: UUID
    operation_claim: UUID
    operation_digest: str = Field(max_length=64)
    source_video_id: str = Field(max_length=128)
    source_evidence: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    source_mid: str | None = Field(default=None, max_length=128)
    status: str = "pending"
    revision: int


class MaterialShareBatchReceipt(SQLModel, table=True):
    __tablename__ = "material_share_batch_receipt"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "batch_id"],
            [
                "material_share_batch.tenant_id",
                "material_share_batch.bc_id",
                "material_share_batch.id",
            ],
        ),
        Index("ix_material_share_receipt_batch", "tenant_id", "batch_id"),
        CheckConstraint(
            "effect IN ('ACKNOWLEDGED','UNKNOWN','NOT_SENT','FAILED')",
            name="ck_material_share_receipt_effect",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    batch_id: UUID
    effect: str = Field(max_length=32)
    code: str | None = Field(default=None, max_length=128)
    share_response: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
