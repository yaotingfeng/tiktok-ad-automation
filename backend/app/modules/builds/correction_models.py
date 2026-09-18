"""独立补建核实台账；不覆盖原创建尝试、请求或远端 ID。"""

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

from app.modules.builds.execution_models import submission_fk


class VerifiedReplacement(SQLModel, table=True):
    __tablename__ = "build_verified_replacement"
    __table_args__ = (
        submission_fk(),
        ForeignKeyConstraint(
            ["tenant_id", "submission_id", "source_step_id"],
            [
                "execution_step.tenant_id",
                "execution_step.submission_id",
                "execution_step.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "submission_id", "source_group_step_id"],
            [
                "execution_step.tenant_id",
                "execution_step.submission_id",
                "execution_step.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_step_id", "source_attempt", "source_attempt_id"],
            [
                "build_attempt_context.tenant_id",
                "build_attempt_context.step_id",
                "build_attempt_context.attempt",
                "build_attempt_context.attempt_id",
            ],
        ),
        UniqueConstraint("tenant_id", "request_id", name="uq_replacement_request"),
        UniqueConstraint("tenant_id", "source_step_id", name="uq_replacement_source"),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "advertiser_id",
            "remote_ad_id",
            name="uq_replacement_remote_ad",
        ),
        CheckConstraint(
            "status = 'VERIFIED_REPLACEMENT'", name="ck_replacement_status"
        ),
        CheckConstraint(
            "length(source_request_digest)=64 AND length(intent_digest)=64 AND length(request_digest)=64",
            name="ck_replacement_digests",
        ),
        CheckConstraint(
            "length(trim(remote_ad_id))>0 AND length(trim(remote_adgroup_id))>0 AND length(trim(original_adgroup_id))>0",
            name="ck_replacement_remote_ids",
        ),
        Index(
            "ix_replacement_submission", "tenant_id", "submission_id", "source_step_id"
        ),
        Index("ix_replacement_group", "tenant_id", "source_group_step_id"),
        Index(
            "ix_replacement_remote_group",
            "tenant_id",
            "bc_id",
            "advertiser_id",
            "remote_adgroup_id",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    submission_id: UUID
    preview_id: UUID
    advertiser_id: str = Field(max_length=128)
    request_id: UUID
    actor_id: UUID = Field(foreign_key="user.id")
    source_step_id: UUID
    source_group_step_id: UUID
    source_attempt: int
    source_attempt_id: UUID
    status: str = "VERIFIED_REPLACEMENT"
    original_adgroup_id: str = Field(max_length=255)
    remote_adgroup_id: str = Field(max_length=255)
    remote_ad_id: str = Field(max_length=255)
    source_request_digest: str = Field(max_length=64)
    intent_digest: str = Field(max_length=64)
    request_digest: str = Field(max_length=64)
    audit_reference: str = Field(max_length=1024)
    route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    original_intent: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    replacement_intent: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    verification: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    verified_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
