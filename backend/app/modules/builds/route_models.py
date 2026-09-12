"""不可变父路由与 attempt 伴随身份；不回写既有追加式远端证据。"""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlmodel import Field, SQLModel


class BuildRouteContext(SQLModel, table=True):
    __tablename__ = "build_route_context"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id"],
            ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
            name="fk_build_route_preview",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "connection_id"],
            [
                "bc_connection_binding.tenant_id",
                "bc_connection_binding.bc_id",
                "bc_connection_binding.connection_id",
            ],
            name="fk_build_route_binding",
        ),
        CheckConstraint(
            "channel IN ('OFFICIAL_API','OFFICIAL_MCP')", name="ck_build_route_channel"
        ),
        CheckConstraint(
            "authorization_revision >= 0 AND length(trim(adapter_contract_revision)) > 0",
            name="ck_build_route_revisions",
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    preview_id: UUID = Field(primary_key=True)
    bc_id: str = Field(max_length=128)
    connection_id: UUID
    channel: str = Field(max_length=32)
    authorization_revision: int
    binding_revision: int = 0
    adapter_contract_revision: str = Field(max_length=128)


class BuildAttemptContext(SQLModel, table=True):
    __tablename__ = "build_attempt_context"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "submission_id", "step_id"],
            [
                "execution_step.tenant_id",
                "execution_step.submission_id",
                "execution_step.id",
            ],
            name="fk_build_attempt_step",
        ),
        UniqueConstraint(
            "tenant_id",
            "submission_id",
            "step_id",
            "attempt",
            name="uq_build_attempt_evidence_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "step_id",
            "attempt",
            "attempt_id",
            name="uq_build_attempt_current_scope",
        ),
        UniqueConstraint("attempt_id", name="uq_build_attempt_id"),
        CheckConstraint("attempt >= 0", name="ck_build_attempt_count"),
    )
    tenant_id: UUID = Field(primary_key=True)
    step_id: UUID = Field(primary_key=True)
    attempt: int = Field(primary_key=True)
    submission_id: UUID
    attempt_id: UUID
