"""广告管理授权证据，与搭建/上传能力保持独立代数。"""

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


def utcnow() -> datetime:
    return datetime.now(UTC)


class ManagementCapability(SQLModel, table=True):
    __tablename__ = "management_capability"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "advertiser_id"],
            ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "bc_id",
                "connection_id",
                "authorization_revision",
                "binding_revision",
            ],
            [
                "bc_connection_binding.tenant_id",
                "bc_connection_binding.bc_id",
                "bc_connection_binding.connection_id",
                "bc_connection_binding.authorization_revision",
                "bc_connection_binding.revision",
            ],
            name="fk_management_capability_binding_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "authorization_revision"],
            [
                "connection_authorization.tenant_id",
                "connection_authorization.connection_id",
                "connection_authorization.authorization_revision",
            ],
            name="fk_management_capability_authorization_revision",
        ),
        CheckConstraint(
            "authorization_revision >= 0 AND binding_revision >= 0",
            name="ck_management_capability_revisions",
        ),
        CheckConstraint(
            "length(trim(adapter_contract_revision)) > 0",
            name="ck_management_capability_contract",
        ),
        CheckConstraint(
            "operation IN ('update_roas','update_budget','set_status','set_material_status')",
            name="ck_management_capability_operation",
        ),
        CheckConstraint(
            "entity_kind IN ('campaign','adgroup','ad','creative','material','account')",
            name="ck_management_capability_entity_kind",
        ),
        CheckConstraint(
            "state IN ('VERIFIED','UNKNOWN','REVOKED')",
            name="ck_management_capability_state",
        ),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "advertiser_id",
            "connection_id",
            "authorization_revision",
            "binding_revision",
            "adapter_contract_revision",
            "operation",
            "entity_kind",
            name="uq_management_capability_key",
        ),
        Index(
            "ix_management_capability_lookup",
            "tenant_id",
            "bc_id",
            "advertiser_id",
            "connection_id",
            "state",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    bc_id: str = Field(max_length=128)
    advertiser_id: str = Field(max_length=128)
    connection_id: UUID
    authorization_revision: int = 0
    binding_revision: int = 0
    adapter_contract_revision: str = Field(max_length=128)
    operation: str = Field(max_length=32)
    entity_kind: str = Field(max_length=16)
    state: str = Field(default="UNKNOWN", max_length=16)
    verified_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    # 只保留脱敏 scope、角色及工具/接口摘要，禁止保存 token 或原始响应。
    evidence: dict = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
