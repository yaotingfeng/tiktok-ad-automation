from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    text,
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
        UniqueConstraint("tenant_id", "id", name="uq_authorization_attempt_tenant_id"),
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


class TenantBC(SQLModel, table=True):
    __tablename__ = "tenant_bc"
    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    name: str = ""
    ownership_conflict: bool = False


class AdvertiserAccount(SQLModel, table=True):
    __tablename__ = "advertiser_account"
    __table_args__ = (
        Index("ix_advertiser_tenant_name_id", "tenant_id", "name", "advertiser_id"),
    )
    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    name: str = ""
    currency: str = ""
    timezone: str = ""
    remote_status: str = "UNKNOWN"
    ownership_conflict: bool = False


class DiscoveryRun(SQLModel, table=True):
    __tablename__ = "discovery_run"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_discovery_run_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_attempt_id"],
            ["authorization_attempt.tenant_id", "authorization_attempt.id"],
        ),
        CheckConstraint(
            "status IN ('RUNNING','ADMISSION_WAIT','ERROR','COMPLETE','CANCELLED')",
            name="ck_discovery_status",
        ),
        Index(
            "uq_discovery_active_connection",
            "tenant_id",
            "connection_id",
            unique=True,
            postgresql_where=text("status IN ('RUNNING','ADMISSION_WAIT')"),
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    actor_id: UUID = Field(foreign_key="user.id")
    connection_id: UUID
    candidate_attempt_id: UUID | None = None
    credential_version: int = 0
    status: str = "RUNNING"
    bc_cursor: str | None = None
    next_attempt_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error_code: str | None = None
    # Each dispatch performs exactly one stage request, with durable input/output.
    work: dict = Field(
        default_factory=lambda: {
            "stage": "AUTHORIZED",
            "page": 1,
            "bc_ids": [],
            "authorized_ids": [],
        },
        sa_column=Column(JSON, nullable=False),
    )
    revision: int = 0
    claim_id: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    sent_count: int = 0
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class DiscoverySeen(SQLModel, table=True):
    __tablename__ = "discovery_seen"
    run_id: UUID = Field(foreign_key="discovery_run.id", primary_key=True)
    # Empty bc_id is reserved for BC enumeration pages.
    bc_id: str = Field(primary_key=True, max_length=128)
    page: int = Field(primary_key=True)
    last_page: bool = False
    processed_count: int = 0
    __table_args__ = (
        CheckConstraint(
            "page > 0 AND processed_count >= 0", name="ck_discovery_seen_page"
        ),
    )


class BCAccountAccess(SQLModel, table=True):
    __tablename__ = "bc_account_access"
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
            ["tenant_id", "last_seen_run_id"],
            ["discovery_run.tenant_id", "discovery_run.id"],
        ),
        Index(
            "ix_access_tenant_bc_active_id",
            "tenant_id",
            "bc_id",
            "active",
            "advertiser_id",
        ),
        CheckConstraint(
            "permission_state IN ('UNKNOWN','VERIFIED','METADATA_INCOMPLETE')",
            name="ck_access_permission",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    connection_id: UUID = Field(primary_key=True)
    in_bc: bool = False
    authorized: bool = False
    active: bool = False
    can_upload: bool = False
    can_build: bool = False
    permission_state: str = "UNKNOWN"
    last_seen_run_id: UUID | None = None
    checked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class ExternalAssetOwner(SQLModel, table=True):
    __tablename__ = "external_asset_owner"
    __table_args__ = (
        CheckConstraint("kind IN ('BC','ADVERTISER')", name="ck_external_asset_kind"),
    )
    kind: str = Field(primary_key=True, max_length=16)
    external_id: str = Field(primary_key=True, max_length=128)
    owner_tenant_id: UUID = Field(foreign_key="tenant.id")
