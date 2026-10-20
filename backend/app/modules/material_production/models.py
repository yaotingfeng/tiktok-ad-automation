from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Column, DateTime, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class MaterialProductionBinding(SQLModel, table=True):
    __tablename__ = "material_production_binding"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','ready','failed')",
            name="ck_material_production_status",
        ),
        UniqueConstraint("request_id", name="uq_material_production_request"),
        UniqueConstraint("username", name="uq_material_production_username"),
        UniqueConstraint("company_id", name="uq_material_production_company"),
        UniqueConstraint("external_user_id", name="uq_material_production_user"),
    )

    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    actor_id: UUID = Field(foreign_key="user.id")
    administrator_id: UUID = Field(foreign_key="user.id")
    request_id: UUID
    request_digest: str = Field(max_length=64)
    username: str = Field(max_length=64)
    key_ref: str = Field(max_length=128)
    key_fingerprint: str = Field(max_length=64)
    request_ciphertext: str | None = Field(default=None, sa_column=Column(Text))
    api_key_ciphertext: str | None = Field(default=None, sa_column=Column(Text))
    company_id: str | None = Field(default=None, max_length=255)
    external_user_id: str | None = Field(default=None, max_length=255)
    api_key_id: str | None = Field(default=None, max_length=255)
    status: str = Field(default="pending", max_length=16)
    error_code: str | None = Field(default=None, max_length=64)
    attempts: int = 0
    dispatch_sequence: int = 0
    processed_sequence: int = 0
    next_attempt_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
