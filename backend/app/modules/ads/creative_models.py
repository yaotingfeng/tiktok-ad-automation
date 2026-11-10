"""平台创意的持久身份：历史删除不删除映射，轮播不选择首图代替创意。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, Column, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import account_reference


class AdCreative(SQLModel, table=True):
    __tablename__ = "ad_creative"
    __table_args__ = (
        account_reference(),
        CheckConstraint("published_version > 0", name="ck_ad_creative_version"),
        CheckConstraint(
            "identity_status IN ('RESOLVED','COMPOSITE','UNRESOLVED','CONFLICT')",
            name="ck_ad_creative_identity_status",
        ),
        CheckConstraint("jsonb_typeof(assets) = 'array'", name="ck_ad_creative_assets"),
        Index(
            "ix_ad_creative_material",
            "tenant_id",
            "platform_material_id",
            "advertiser_id",
        ),
        Index("ix_ad_creative_parent", "tenant_id", "advertiser_id", "ad_remote_id"),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    creative_id: str = Field(primary_key=True, max_length=128)
    ad_remote_id: str = Field(max_length=128)
    ad_material_id: str | None = Field(default=None, max_length=128)
    material_type: str | None = Field(default=None, max_length=64)
    platform_material_id: str | None = Field(default=None, max_length=128)
    assets: list[dict[str, str]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    name: str = ""
    operation_status: str | None = None
    deleted: bool = False
    identity_status: str = Field(max_length=16)
    first_observed_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    observed_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    published_version: int
    evidence: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
