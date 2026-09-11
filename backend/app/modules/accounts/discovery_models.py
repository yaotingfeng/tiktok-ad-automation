"""候选发现的不可执行暂存页；完整发布前不修改 live 账户与授权。"""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKeyConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

DiscoveryStage = Literal["SUBJECT", "AUTHORIZED", "BCS", "ASSETS", "DETAILS", "ROLES"]
PaginationKind = Literal["REMOTE", "FULL_RESPONSE", "EXPLICIT_IDS"]


class DiscoveryStagedPage(SQLModel, table=True):
    __tablename__ = "discovery_staged_page"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "run_id"],
            [
                "discovery_run.tenant_id",
                "discovery_run.connection_id",
                "discovery_run.id",
            ],
            name="fk_discovery_staged_page_scope",
        ),
        CheckConstraint(
            "stage IN ('SUBJECT','AUTHORIZED','BCS','ASSETS','DETAILS','ROLES')",
            name="ck_discovery_staged_page_stage",
        ),
        CheckConstraint(
            "pagination_kind IN ('REMOTE','FULL_RESPONSE','EXPLICIT_IDS')",
            name="ck_discovery_staged_page_pagination_kind",
        ),
        CheckConstraint(
            "page >= 1 AND total_pages >= 1 AND page <= total_pages AND "
            "(total_number IS NULL OR total_number >= 0) AND "
            "last_page = (page = total_pages)",
            name="ck_discovery_staged_page_numbers",
        ),
        CheckConstraint(
            "jsonb_typeof(rows) = 'array' AND jsonb_array_length(rows) <= 50 AND "
            "jsonb_typeof(call_evidence) = 'object'",
            name="ck_discovery_staged_page_payload",
        ),
        CheckConstraint(
            "schema_digest ~ '^[0-9a-f]{64}$'",
            name="ck_discovery_staged_page_schema_digest",
        ),
        Index("ix_discovery_staged_page_tenant_run", "tenant_id", "run_id"),
    )
    tenant_id: UUID
    connection_id: UUID
    run_id: UUID = Field(primary_key=True)
    bc_id: str = Field(default="", primary_key=True, max_length=128)
    stage: str = Field(primary_key=True, max_length=16)
    page: int = Field(primary_key=True)
    total_pages: int
    total_number: int | None = None
    last_page: bool
    pagination_kind: str = Field(max_length=16)
    rows: list[dict[str, Any]] = Field(
        sa_column=Column(JSONB, nullable=False), repr=False
    )
    call_evidence: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    schema_digest: str = Field(max_length=64)
    observed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
