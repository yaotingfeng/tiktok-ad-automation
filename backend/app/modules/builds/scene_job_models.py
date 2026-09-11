"""Shared account/application scene jobs and bounded draft dependencies."""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class SceneJob(SQLModel, table=True):
    __tablename__ = "build_scene_job"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_build_scene_job_tenant"),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "advertiser_id",
            "id",
            name="uq_build_scene_job_account",
        ),
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
            ["tenant_id", "provider_connection_id", "application_id"],
            [
                "provider_application.tenant_id",
                "provider_application.connection_id",
                "provider_application.external_id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "capability_job_id"],
            [
                "account_capability_job.tenant_id",
                "account_capability_job.bc_id",
                "account_capability_job.id",
            ],
        ),
        CheckConstraint(
            "status IN ('PENDING','COMPLETE','BLOCKED','STALE','FAILED')",
            name="ck_build_scene_job_status",
        ),
        CheckConstraint(
            "resource IN ('capabilities','identity','minis','cta','vbo','regions','done')",
            name="ck_build_scene_job_resource",
        ),
        CheckConstraint(
            "revision >= 0 AND next_page > 0 AND failure_count >= 0 AND credential_revision >= 0",
            name="ck_build_scene_job_counters",
        ),
        CheckConstraint(
            "(claim_token IS NULL) = (claimed_until IS NULL)",
            name="ck_build_scene_job_claim",
        ),
        CheckConstraint(
            "(first_observed_at IS NULL) = (expires_at IS NULL)",
            name="ck_build_scene_job_observation",
        ),
        CheckConstraint(
            "jsonb_typeof(facts) = 'object'", name="ck_build_scene_job_facts"
        ),
        Index(
            "uq_build_scene_job_active_basis",
            "tenant_id",
            "scope_basis",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "ix_build_scene_job_latest", "tenant_id", "scope_basis", "created_at", "id"
        ),
        Index("ix_build_scene_job_repair", "status", "repair_after", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    actor_id: UUID = Field(foreign_key="user.id")
    bc_id: str = Field(max_length=128)
    advertiser_id: str = Field(max_length=128)
    connection_id: UUID
    credential_revision: int
    provider_connection_id: UUID
    application_id: str = Field(max_length=255)
    minis_id: str = Field(max_length=255)
    scope_basis: str = Field(max_length=64)
    capability_job_id: UUID | None = None
    status: str = Field(default="PENDING", max_length=16)
    resource: str = Field(default="capabilities", max_length=32)
    next_page: int = 1
    facts: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    revision: int = 0
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    due_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    repair_after: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    first_observed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error_code: str | None = Field(default=None, max_length=64)
    failure_count: int = 0


class SceneJobPage(SQLModel, table=True):
    __tablename__ = "build_scene_job_page"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "job_id"], ["build_scene_job.tenant_id", "build_scene_job.id"]
        ),
        UniqueConstraint("job_id", "resource", "page", name="uq_build_scene_job_page"),
        CheckConstraint("page > 0", name="ck_build_scene_job_page_number"),
        CheckConstraint(
            "resource IN ('identity','minis','cta','vbo','regions')",
            name="ck_build_scene_job_page_resource",
        ),
        CheckConstraint(
            "jsonb_typeof(facts) = 'object'", name="ck_build_scene_job_page_facts"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    job_id: UUID
    resource: str = Field(max_length=32)
    page: int
    endpoint: str = Field(max_length=255)
    request_id: str | None = Field(default=None, max_length=128)
    source_revision: str = Field(max_length=64)
    scope_basis: str = Field(max_length=64)
    facts: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    observed_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class DraftScenePreparation(SQLModel, table=True):
    __tablename__ = "draft_scene_preparation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "preparation_id"],
            [
                "draft_preparation.tenant_id",
                "draft_preparation.draft_id",
                "draft_preparation.id",
            ],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "draft_id",
            "preparation_id",
            name="uq_draft_scene_preparation_scope",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    preparation_id: UUID = Field(primary_key=True)
    draft_id: UUID
    connection_after: UUID | None = None
    capabilities_queued: bool = False
    capabilities_complete: bool = False
    scene_after: str | None = Field(default=None, max_length=128)
    scenes_queued: bool = False


class DraftCapabilityDependency(SQLModel, table=True):
    __tablename__ = "draft_capability_dependency"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "preparation_id"],
            [
                "draft_scene_preparation.tenant_id",
                "draft_scene_preparation.draft_id",
                "draft_scene_preparation.preparation_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "job_id"],
            [
                "account_capability_job.tenant_id",
                "account_capability_job.bc_id",
                "account_capability_job.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
        ),
        CheckConstraint(
            "status IN ('PENDING','COMPLETE','BLOCKED')",
            name="ck_draft_capability_dependency_status",
        ),
        Index(
            "ix_draft_capability_dependency_pending",
            "tenant_id",
            "preparation_id",
            "status",
            "connection_id",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    preparation_id: UUID = Field(primary_key=True)
    connection_id: UUID = Field(primary_key=True)
    draft_id: UUID
    bc_id: str = Field(max_length=128)
    job_id: UUID
    status: str = Field(default="PENDING", max_length=16)
    error_code: str | None = Field(default=None, max_length=64)


class DraftSceneDependency(SQLModel, table=True):
    __tablename__ = "draft_scene_dependency"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "preparation_id"],
            [
                "draft_scene_preparation.tenant_id",
                "draft_scene_preparation.draft_id",
                "draft_scene_preparation.preparation_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id", "advertiser_id"],
            [
                "draft_account.tenant_id",
                "draft_account.draft_id",
                "draft_account.advertiser_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "advertiser_id", "job_id"],
            [
                "build_scene_job.tenant_id",
                "build_scene_job.bc_id",
                "build_scene_job.advertiser_id",
                "build_scene_job.id",
            ],
        ),
        CheckConstraint(
            "status IN ('PENDING','COMPLETE','BLOCKED')",
            name="ck_draft_scene_dependency_status",
        ),
        Index(
            "ix_draft_scene_dependency_pending",
            "tenant_id",
            "preparation_id",
            "status",
            "advertiser_id",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    preparation_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    draft_id: UUID
    bc_id: str = Field(max_length=128)
    job_id: UUID | None = None
    status: str = Field(default="PENDING", max_length=16)
    error_code: str | None = Field(default=None, max_length=64)
