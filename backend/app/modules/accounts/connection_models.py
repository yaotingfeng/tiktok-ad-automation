"""TikTok 双通道的授权事实、候选状态与明确 BC 路由。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.integrations.tiktok.contracts.context import ChannelKind


def utcnow() -> datetime:
    return datetime.now(UTC)


class McpAuthorizationAttempt(SQLModel, table=True):
    __tablename__ = "mcp_authorization_attempt"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "connection_id",
            "id",
            name="uq_mcp_authorization_attempt_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["tenant_membership.tenant_id", "tenant_membership.user_id"],
        ),
        CheckConstraint(
            "base_credential_revision >= 0 AND base_authorization_revision >= 0",
            name="ck_mcp_authorization_attempt_revisions",
        ),
        CheckConstraint(
            "status IN ('PENDING','CLAIMED','CANDIDATE_READY','RESULT_UNKNOWN','CANCELLED','FAILED','ACCEPTED')",
            name="ck_mcp_authorization_attempt_status",
        ),
        CheckConstraint(
            "(claim_id IS NULL) = (claimed_until IS NULL)",
            name="ck_mcp_authorization_attempt_claim",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    actor_id: UUID
    connection_id: UUID
    base_credential_revision: int = 0
    base_authorization_revision: int = 0
    issuer: str = Field(max_length=2048)
    resource: str = Field(max_length=2048)
    redirect_uri: str = Field(max_length=2048)
    state_hash: str = Field(unique=True, max_length=64, repr=False)
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    status: str = Field(default="PENDING", max_length=32)
    # PKCE 与候选令牌仅保存加密封套，不存授权码/完整回调 URL。
    pkce_verifier_ciphertext: str | None = Field(default=None, repr=False)
    candidate_ciphertext: str | None = Field(default=None, repr=False)
    claim_id: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    claimed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error_code: str | None = Field(default=None, max_length=128)


class ConnectionAuthorization(SQLModel, table=True):
    __tablename__ = "connection_authorization"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "connection_id", "id", name="uq_connection_authorization_scope"
        ),
        UniqueConstraint(
            "tenant_id",
            "connection_id",
            "authorization_revision",
            name="uq_connection_authorization_revision",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "previous_authorization_id"],
            [
                "connection_authorization.tenant_id",
                "connection_authorization.connection_id",
                "connection_authorization.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "mcp_authorization_attempt_id"],
            [
                "mcp_authorization_attempt.tenant_id",
                "mcp_authorization_attempt.connection_id",
                "mcp_authorization_attempt.id",
            ],
        ),
        CheckConstraint(
            "authorization_revision >= 0", name="ck_connection_authorization_revision"
        ),
        CheckConstraint(
            "jsonb_typeof(scopes) = 'array' AND jsonb_typeof(permission_summary) = 'object'",
            name="ck_connection_authorization_facts",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    connection_id: UUID
    authorization_revision: int
    # 官方未返回的主体/grant 必须保持未知；本地连续关系不能冒充上游 ID。
    upstream_subject: str | None = Field(default=None, max_length=512)
    upstream_grant_id: str | None = Field(default=None, max_length=512)
    issuer: str | None = Field(default=None, max_length=2048)
    resource: str | None = Field(default=None, max_length=2048)
    scopes: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    permission_summary: dict = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    source: str = Field(default="UNKNOWN", max_length=128)
    verified_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    access_token_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    previous_authorization_id: UUID | None = None
    mcp_authorization_attempt_id: UUID | None = None
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ConnectionToolObservation(SQLModel, table=True):
    __tablename__ = "connection_tool_observation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "candidate_attempt_id"],
            [
                "mcp_authorization_attempt.tenant_id",
                "mcp_authorization_attempt.connection_id",
                "mcp_authorization_attempt.id",
            ],
        ),
        CheckConstraint(
            "jsonb_typeof(tool_schemas) = 'object' AND jsonb_typeof(call_evidence) = 'object'",
            name="ck_connection_tool_observation_evidence",
        ),
        Index(
            "ix_connection_tool_observation_scope",
            "tenant_id",
            "connection_id",
            "observed_at",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    connection_id: UUID
    candidate_attempt_id: UUID | None = None
    schema_digest: str = Field(max_length=64)
    expected_contract_revision: str = Field(max_length=128)
    pagination_complete: bool = False
    tool_schemas: dict = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    # 仅保存脱敏调用关联和结构摘要；工具存在或目录读取不等于拥有写权限。
    call_evidence: dict = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    observed_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class McpRefreshAttempt(SQLModel, table=True):
    __tablename__ = "mcp_refresh_attempt"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        CheckConstraint(
            "base_credential_revision >= 0 AND base_authorization_revision >= 0",
            name="ck_mcp_refresh_attempt_revisions",
        ),
        CheckConstraint(
            "status IN ('PENDING','CLAIMED','REQUEST_ARMED','CANDIDATE_READY','PUBLISHED','OUTCOME_UNKNOWN','REJECTED','SUPERSEDED')",
            name="ck_mcp_refresh_attempt_status",
        ),
        CheckConstraint(
            "(claim_id IS NULL) = (claimed_until IS NULL)",
            name="ck_mcp_refresh_attempt_claim",
        ),
        # 未知结果也占用当前凭据修订，禁止并发重放可能已消费的 refresh token。
        Index(
            "uq_mcp_refresh_active_revision",
            "tenant_id",
            "connection_id",
            "base_credential_revision",
            unique=True,
            postgresql_where=text(
                "status IN ('PENDING','CLAIMED','REQUEST_ARMED','CANDIDATE_READY','OUTCOME_UNKNOWN')"
            ),
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id", index=True)
    connection_id: UUID
    base_credential_revision: int
    base_authorization_revision: int
    status: str = Field(default="PENDING", max_length=32)
    claim_id: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    request_armed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    candidate_ciphertext: str | None = Field(default=None, repr=False)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error_code: str | None = Field(default=None, max_length=128)


class BCConnectionBinding(SQLModel, table=True):
    __tablename__ = "bc_connection_binding"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "kind"],
            [
                "tiktok_connection.tenant_id",
                "tiktok_connection.id",
                "tiktok_connection.kind",
            ],
        ),
        Index(
            "uq_mcp_one_bc",
            "tenant_id",
            "connection_id",
            unique=True,
            postgresql_where=text("kind = 'OFFICIAL_MCP'"),
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    connection_id: UUID = Field(primary_key=True)
    kind: ChannelKind = Field(sa_type=String(32))


class BCDefaultRoute(SQLModel, table=True):
    __tablename__ = "bc_default_route"
    __table_args__ = (
        # 默认只能指向已存在的同租户、同 BC 绑定，不能借用其他租户连接。
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "connection_id"],
            [
                "bc_connection_binding.tenant_id",
                "bc_connection_binding.bc_id",
                "bc_connection_binding.connection_id",
            ],
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    connection_id: UUID
