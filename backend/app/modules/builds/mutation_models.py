from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKeyConstraint
from sqlmodel import Field, SQLModel


class DraftMutationRequest(SQLModel, table=True):
    __tablename__ = "draft_mutation_request"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "draft_id"], ["build_draft.tenant_id", "build_draft.id"]
        ),
        CheckConstraint(
            "kind IN ('update','groups') AND applied_revision > 0",
            name="ck_draft_mutation_result",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    request_id: UUID = Field(primary_key=True)
    draft_id: UUID
    actor_id: UUID = Field(foreign_key="user.id")
    kind: str = Field(max_length=16)
    request_digest: str = Field(max_length=64)
    applied_revision: int
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
