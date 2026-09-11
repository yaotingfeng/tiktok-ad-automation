"""新授权只读核查的独立审计；原执行请求和后续创建状态始终保持原样。"""

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


def _route_shape(column: str) -> str:
    # 固定模型列名；六字段、严格JSON类型与整数修订，不接受额外键或字符串数字。
    keys = "ARRAY['tenant_id','bc_id','connection_id','channel','authorization_revision','adapter_contract_revision']"
    strings = (
        "tenant_id",
        "bc_id",
        "connection_id",
        "channel",
        "adapter_contract_revision",
    )
    return " AND ".join(
        [
            f"jsonb_typeof({column})='object'",
            f"{column} ?& {keys}",
            f"{column} - {keys} = '{{}}'::jsonb",
            *(f"jsonb_typeof({column}->'{key}')='string'" for key in strings),
            f"jsonb_typeof({column}->'authorization_revision')='number'",
            f"{column}->>'authorization_revision' ~ '^[0-9]+$'",
        ]
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


class BuildHistoricalRead(SQLModel, table=True):
    __tablename__ = "build_historical_read"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_historical_read_tenant"),
        UniqueConstraint("tenant_id", "request_id", name="uq_historical_read_request"),
        ForeignKeyConstraint(
            ["tenant_id", "submission_id", "source_step_id"],
            [
                "execution_step.tenant_id",
                "execution_step.submission_id",
                "execution_step.id",
            ],
            name="fk_historical_read_source",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_step_id", "source_attempt", "source_attempt_id"],
            [
                "build_attempt_context.tenant_id",
                "build_attempt_context.step_id",
                "build_attempt_context.attempt",
                "build_attempt_context.attempt_id",
            ],
            name="fk_historical_read_attempt",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "connection_id"],
            [
                "bc_connection_binding.tenant_id",
                "bc_connection_binding.bc_id",
                "bc_connection_binding.connection_id",
            ],
            name="fk_historical_read_binding",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "old_authorization_revision"],
            [
                "connection_authorization.tenant_id",
                "connection_authorization.connection_id",
                "connection_authorization.authorization_revision",
            ],
            name="fk_historical_read_old_authorization",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "new_authorization_revision"],
            [
                "connection_authorization.tenant_id",
                "connection_authorization.connection_id",
                "connection_authorization.authorization_revision",
            ],
            name="fk_historical_read_new_authorization",
        ),
        CheckConstraint(
            "old_authorization_revision>=0 AND new_authorization_revision>old_authorization_revision AND source_attempt>=0",
            name="ck_historical_read_revisions",
        ),
        CheckConstraint(
            "source_request_digest ~ '^[0-9a-f]{64}$'", name="ck_historical_read_digest"
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','CONFIRMED','UNKNOWN','BLOCKED')",
            name="ck_historical_read_status",
        ),
        CheckConstraint(
            "(claim_token IS NULL)=(claimed_until IS NULL) AND dispatch_revision>=0",
            name="ck_historical_read_claim",
        ),
        CheckConstraint(
            "jsonb_typeof(old_route)='object' AND jsonb_typeof(new_route)='object' AND octet_length(old_route::text)<=8192 AND octet_length(new_route::text)<=8192",
            name="ck_historical_read_routes",
        ),
        CheckConstraint(
            "coalesce(("
            + _route_shape("old_route")
            + " AND "
            + _route_shape("new_route")
            + "),false)",
            name="ck_historical_read_route_shape",
        ),
        CheckConstraint(
            "coalesce(old_route->>'tenant_id'=tenant_id::text AND new_route->>'tenant_id'=tenant_id::text "
            "AND old_route->>'connection_id'=connection_id::text AND new_route->>'connection_id'=connection_id::text "
            "AND old_route->>'bc_id'=bc_id AND new_route->>'bc_id'=bc_id "
            "AND old_route->>'authorization_revision'=old_authorization_revision::text "
            "AND new_route->>'authorization_revision'=new_authorization_revision::text "
            "AND old_route->>'channel'=new_route->>'channel' "
            "AND old_route->>'channel' IN ('OFFICIAL_API','OFFICIAL_MCP') "
            "AND length(trim(old_route->>'adapter_contract_revision'))>0 "
            "AND length(trim(new_route->>'adapter_contract_revision'))>0,false)",
            name="ck_historical_read_route_scope",
        ),
        CheckConstraint(
            "jsonb_typeof(authorization_proof)='object' AND octet_length(authorization_proof::text)<=16384",
            name="ck_historical_read_proof",
        ),
        CheckConstraint(
            "jsonb_typeof(progress)='object' AND octet_length(progress::text)<=262144",
            name="ck_historical_read_progress",
        ),
        Index("ix_historical_read_repair", "status", "repair_after", "id"),
        Index(
            "ix_historical_read_source",
            "tenant_id",
            "source_step_id",
            "created_at",
            "id",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    request_id: UUID
    submission_id: UUID
    source_step_id: UUID
    source_attempt: int
    source_attempt_id: UUID
    connection_id: UUID
    bc_id: str = Field(max_length=128)
    advertiser_id: str = Field(max_length=128)
    old_authorization_revision: int
    new_authorization_revision: int
    request_actor_id: UUID = Field(foreign_key="user.id")
    source_request_digest: str = Field(max_length=64)
    old_route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    new_route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    authorization_proof: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    status: str = Field(default="PENDING", max_length=16)
    progress: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    dispatch_revision: int = 0
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    due_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    repair_after: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error_code: str | None = Field(default=None, max_length=128)
    remote_id: str | None = Field(default=None, max_length=255)
    mismatch: bool = False


class BuildHistoricalReadPage(SQLModel, table=True):
    __tablename__ = "build_historical_read_page"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "read_id"],
            ["build_historical_read.tenant_id", "build_historical_read.id"],
            name="fk_historical_page_read",
        ),
        UniqueConstraint("read_id", "claim_token", name="uq_historical_page_claim"),
        CheckConstraint(
            "stage IN ('OBJECT','STATUS') AND page BETWEEN 1 AND 1000",
            name="ck_historical_page_position",
        ),
        CheckConstraint(
            "jsonb_typeof(ids)='array' AND jsonb_array_length(ids)<=100",
            name="ck_historical_page_ids",
        ),
        CheckConstraint(
            "jsonb_typeof(call_evidence)='object' AND octet_length(call_evidence::text)<=16384",
            name="ck_historical_page_call",
        ),
        CheckConstraint(
            "jsonb_typeof(summary)='object' AND octet_length(summary::text)<=262144",
            name="ck_historical_page_summary",
        ),
        Index(
            "ix_historical_page_scan",
            "tenant_id",
            "read_id",
            "scan_id",
            "stage",
            "page",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    read_id: UUID
    claim_token: UUID
    scan_id: UUID
    stage: str = Field(max_length=16)
    page: int
    ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    call_evidence: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    summary: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    observed_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
