from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel

from app.integrations.tiktok.contracts.context import ChannelKind

# discovery 的 MCP 复合外键在普通应用进程也需注册，不能依赖 Alembic 导入。
from app.modules.accounts import connection_models as connection_models
from app.modules.accounts import discovery_models as discovery_models

OPERABLE_REMOTE_STATUSES = frozenset({"STATUS_ENABLE", "ENABLE"})


class TikTokConnection(SQLModel, table=True):
    __tablename__ = "tiktok_connection"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tiktok_connection_tenant_id"),
        UniqueConstraint(
            "tenant_id", "id", "kind", name="uq_tiktok_connection_tenant_kind"
        ),
        CheckConstraint(
            "kind IN ('OFFICIAL_API','OFFICIAL_MCP')", name="ck_tiktok_connection_kind"
        ),
        CheckConstraint(
            "authorization_revision >= 0",
            name="ck_tiktok_connection_authorization_revision",
        ),
        CheckConstraint(
            "status IN ('PENDING_AUTH','DISCOVERING','ACTIVE','REAUTH_REQUIRED','ERROR','DISABLED')",
            name="ck_tiktok_connection_status",
        ),
        CheckConstraint(
            "credential_revision >= 0", name="ck_tiktok_connection_version"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    status: str = Field(default="PENDING_AUTH", max_length=32)
    credential_ciphertext: str | None = Field(default=None, repr=False)
    credential_revision: int = 0
    # 凭据轮换、授权边界和适配契约分别版本化，普通刷新不改变授权语义。
    kind: ChannelKind = Field(
        default="OFFICIAL_API",
        sa_column=Column(String(32), nullable=False, default="OFFICIAL_API"),
    )
    display_name: str = Field(default="", max_length=255)
    service_profile: str | None = Field(default=None, max_length=128)
    authorization_revision: int = 0
    adapter_contract_revision: str = Field(default="official-api-v1", max_length=128)


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
            "base_credential_revision >= 0",
            name="ck_authorization_attempt_base_version",
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
    base_credential_revision: int = 0
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
        UniqueConstraint(
            "tenant_id", "connection_id", "id", name="uq_discovery_run_connection"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_attempt_id"],
            ["authorization_attempt.tenant_id", "authorization_attempt.id"],
        ),
        # MCP 与 API 候选各有独立记录，禁止混填或引用其他连接的候选。
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "mcp_candidate_attempt_id"],
            [
                "mcp_authorization_attempt.tenant_id",
                "mcp_authorization_attempt.connection_id",
                "mcp_authorization_attempt.id",
            ],
            name="fk_discovery_mcp_candidate",
        ),
        CheckConstraint(
            "candidate_attempt_id IS NULL OR mcp_candidate_attempt_id IS NULL",
            name="ck_discovery_candidate_exclusive",
        ),
        CheckConstraint(
            "credential_revision >= 0", name="ck_discovery_credential_revision"
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
            postgresql_where=text(
                "status IN ('RUNNING','ADMISSION_WAIT') AND bc_id IS NULL"
            ),
        ),
        Index(
            "uq_discovery_active_bc",
            "tenant_id",
            "connection_id",
            "bc_id",
            unique=True,
            postgresql_where=text(
                "status IN ('RUNNING','ADMISSION_WAIT') AND bc_id IS NOT NULL"
            ),
        ),
        CheckConstraint(
            "authorization_revision >= 0 AND binding_revision >= 0",
            name="ck_discovery_binding_revisions",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    actor_id: UUID = Field(foreign_key="user.id")
    connection_id: UUID
    candidate_attempt_id: UUID | None = None
    mcp_candidate_attempt_id: UUID | None = None
    credential_revision: int = 0
    status: str = "RUNNING"
    bc_id: str | None = Field(default=None, max_length=128)
    authorization_revision: int = 0
    binding_revision: int = 0
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
