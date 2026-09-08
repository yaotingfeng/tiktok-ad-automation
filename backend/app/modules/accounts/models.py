from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel


class TikTokConnection(SQLModel, table=True):
    __tablename__ = "tiktok_connection"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tiktok_connection_tenant_id"),
        CheckConstraint(
            "status IN ('PENDING_AUTH','DISCOVERING','ACTIVE','REAUTH_REQUIRED','ERROR','DISABLED')",
            name="ck_tiktok_connection_status",
        ),
        CheckConstraint("credential_version >= 0", name="ck_tiktok_connection_version"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    status: str = Field(default="PENDING_AUTH", max_length=32)
    credential_ciphertext: str | None = Field(default=None, repr=False)
    credential_version: int = 0


class AuthorizationAttempt(SQLModel, table=True):
    __tablename__ = "authorization_attempt"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
            name="fk_authorization_attempt_tenant_connection",
        ),
        CheckConstraint(
            "base_credential_version >= 0", name="ck_authorization_attempt_base_version"
        ),
        CheckConstraint(
            "status IN ('PENDING','CLAIMED','CANDIDATE_READY','RESULT_UNKNOWN','CANCELLED','FAILED','ACCEPTED')",
            name="ck_authorization_attempt_status",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    actor_id: UUID = Field(foreign_key="user.id")
    connection_id: UUID = Field(index=True)
    base_credential_version: int = 0
    state_hash: str = Field(unique=True, max_length=64, repr=False)
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    claimed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    status: str = Field(default="PENDING", max_length=32)
    candidate_ciphertext: str | None = Field(default=None, repr=False)
