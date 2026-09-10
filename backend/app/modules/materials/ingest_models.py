"""Tenant-scoped transient originals; durable platform identities live separately.

Helpers never commit. Call them in the same transaction as the evidence they
summarize. Cumulative milestones and current-state transitions are independent.
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlmodel import Field, Session, SQLModel, col, select

from .models import access_reference, material_reference


def utcnow() -> datetime:
    return datetime.now(UTC)


def timestamp(*, nullable: bool = True) -> Any:
    return Column(DateTime(timezone=True), nullable=nullable)


def bigint() -> Any:
    return Column(BigInteger, nullable=False)


def bc_reference() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
    )


def session_reference() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "bc_id", "session_id"],
        ["ingest_session.tenant_id", "ingest_session.bc_id", "ingest_session.id"],
    )


def object_reference() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "bc_id", "material_id", "generation"],
        [
            "temporary_material_object.tenant_id",
            "temporary_material_object.bc_id",
            "temporary_material_object.material_id",
            "temporary_material_object.generation",
        ],
    )


def canonical_object_key(
    tenant_id: UUID, bc_id: str, material_id: UUID, generation: int
) -> str:
    if generation < 1 or not bc_id:
        raise ValueError("object scope and positive generation are required")
    return f"tenants/{tenant_id}/bc/{quote(bc_id, safe='')}/materials/{material_id}/{generation}/original"


class IngestSession(SQLModel, table=True):
    __tablename__ = "ingest_session"
    __table_args__ = (
        bc_reference(),
        ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["tenant_membership.tenant_id", "tenant_membership.user_id"],
        ),
        UniqueConstraint("tenant_id", "bc_id", "id", name="uq_ingest_session_scope"),
        UniqueConstraint("tenant_id", "request_id", name="uq_ingest_session_request"),
        CheckConstraint(
            "expected_files > 0 AND expected_bytes > 0 AND registration_cursor >= 0 AND revision >= 0",
            name="ck_ingest_session_manifest",
        ),
        CheckConstraint(
            "accepted_count >= 0 AND uploaded_count >= 0 AND ready_count >= 0 AND failed_count >= 0 AND cleaned_count >= 0 AND accepted_count <= expected_files AND uploaded_count <= accepted_count AND ready_count <= accepted_count AND cleaned_count <= accepted_count AND failed_count <= accepted_count",
            name="ck_ingest_session_counts",
        ),
        CheckConstraint(
            "accepted_bytes >= 0 AND uploaded_bytes >= 0 AND ready_bytes >= 0 AND cleaned_bytes >= 0 AND accepted_bytes <= expected_bytes AND uploaded_bytes <= accepted_bytes AND ready_bytes <= accepted_bytes AND cleaned_bytes <= accepted_bytes",
            name="ck_ingest_session_bytes",
        ),
        CheckConstraint(
            "reserved_bytes >= 0 AND stored_bytes >= 0 AND stored_bytes <= reserved_bytes",
            name="ck_ingest_session_occupancy",
        ),
        Index("ix_ingest_session_history", "tenant_id", "bc_id", "created_at", "id"),
        Index("ix_ingest_session_recovery", "status", "next_attempt_at", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    actor_id: UUID
    request_id: UUID
    request_digest: str = Field(max_length=64)
    expected_files: int
    expected_bytes: int = Field(sa_column=bigint())
    registration_cursor: int = 0
    status: str = Field(default="registering", max_length=32)
    accepted_count: int = 0
    uploaded_count: int = 0
    ready_count: int = 0
    failed_count: int = 0
    cleaned_count: int = 0
    accepted_bytes: int = Field(default=0, sa_column=bigint())
    uploaded_bytes: int = Field(default=0, sa_column=bigint())
    ready_bytes: int = Field(default=0, sa_column=bigint())
    cleaned_bytes: int = Field(default=0, sa_column=bigint())
    # Current admitted occupancy, separate from cumulative uploaded/cleaned facts.
    reserved_bytes: int = Field(default=0, sa_column=bigint())
    stored_bytes: int = Field(default=0, sa_column=bigint())
    revision: int = 0
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    next_attempt_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )


class IngestChunk(SQLModel, table=True):
    __tablename__ = "ingest_chunk"
    __table_args__ = (
        session_reference(),
        UniqueConstraint(
            "tenant_id", "session_id", "request_id", name="uq_ingest_chunk_request"
        ),
        CheckConstraint(
            "jsonb_array_length(client_indexes) BETWEEN 1 AND 200",
            name="ck_ingest_chunk_bound",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    session_id: UUID
    request_id: UUID
    request_digest: str = Field(max_length=64)
    client_indexes: list[int] = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )


FILE_STATUSES = "'registered','waiting_capacity','receiving','stored','validating','uploading','verifying','available','failed','blocked','result_unknown','cancelled'"


class IngestSessionFile(SQLModel, table=True):
    __tablename__ = "ingest_session_file"
    __table_args__ = (
        session_reference(),
        material_reference(),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "source_advertiser_id", "connection_id"],
            [
                "bc_account_access.tenant_id",
                "bc_account_access.bc_id",
                "bc_account_access.advertiser_id",
                "bc_account_access.connection_id",
            ],
        ),
        UniqueConstraint(
            "tenant_id", "session_id", "client_index", name="uq_ingest_client_index"
        ),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "session_id",
            "material_id",
            name="uq_ingest_file_material",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_ingest_file_scope"),
        CheckConstraint(
            "client_index >= 0 AND byte_size > 0 AND current_generation > 0 AND revision >= 0",
            name="ck_ingest_file_numbers",
        ),
        CheckConstraint(f"status IN ({FILE_STATUSES})", name="ck_ingest_file_status"),
        CheckConstraint(
            "(source_advertiser_id IS NULL) = (connection_id IS NULL)",
            name="ck_ingest_file_source",
        ),
        Index(
            "ix_ingest_file_seek",
            "tenant_id",
            "session_id",
            "client_index",
            "material_id",
        ),
        CheckConstraint(
            "last_modified_ms IS NULL OR last_modified_ms >= 0",
            name="ck_ingest_file_modified",
        ),
        Index(
            "ix_ingest_file_status_seek",
            "tenant_id",
            "session_id",
            "status",
            "client_index",
            "material_id",
        ),
        Index("ix_ingest_file_recovery", "status", "next_attempt_at", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    session_id: UUID
    client_index: int
    material_id: UUID
    byte_size: int = Field(sa_column=bigint())
    manifest_digest: str = Field(max_length=64)
    last_modified_ms: int | None = Field(default=None, sa_column=Column(BigInteger))
    status: str = Field(default="registered", max_length=32)
    current_generation: int = 1
    source_advertiser_id: str | None = Field(default=None, max_length=128)
    connection_id: UUID | None = None
    revision: int = 0
    error_code: str | None = Field(default=None, max_length=128)
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    next_attempt_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )


OBJECT_STATUSES = "'waiting_capacity','reserved','receiving','stored','validating','verified','cleanup_pending','deleting','deleted','delete_unknown','missing'"


class TemporaryMaterialObject(SQLModel, table=True):
    __tablename__ = "temporary_material_object"
    __table_args__ = (
        material_reference(),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "material_id",
            "generation",
            name="uq_temporary_object_generation",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_temporary_object_scope"),
        UniqueConstraint("object_key", name="uq_temporary_object_key"),
        CheckConstraint(
            "generation > 0 AND expected_bytes > 0 AND (actual_bytes IS NULL OR actual_bytes > 0) AND reserved_bytes >= 0 AND revision >= 0 AND part_size > 0",
            name="ck_temporary_object_numbers",
        ),
        CheckConstraint(
            f"status IN ({OBJECT_STATUSES})", name="ck_temporary_object_status"
        ),
        CheckConstraint(
            "(claim_token IS NULL) = (claimed_until IS NULL)",
            name="ck_temporary_object_claim",
        ),
        CheckConstraint(
            "status != 'verified' OR (sha256 IS NOT NULL AND video_md5 IS NOT NULL AND digest_verified_at IS NOT NULL AND actual_bytes IS NOT NULL AND actual_bytes = expected_bytes)",
            name="ck_temporary_object_verified",
        ),
        Index(
            "ix_ingest_transport_recovery",
            "status",
            "next_attempt_at",
            "id",
            postgresql_where=text(
                "error_code IN ('multipart_creating','multipart_create_unknown','multipart_collecting','multipart_completing','multipart_complete_unknown')"
            ),
        ),
        Index("ix_temporary_object_recovery", "status", "next_attempt_at", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    material_id: UUID
    generation: int
    object_key: str = Field(max_length=512)
    storage_provider: str | None = Field(default=None, max_length=32)
    storage_endpoint: str | None = Field(default=None, max_length=512)
    storage_bucket: str | None = Field(default=None, max_length=255)
    expected_bytes: int = Field(sa_column=bigint())
    actual_bytes: int | None = Field(default=None, sa_column=Column(BigInteger))
    reserved_bytes: int = Field(default=0, sa_column=bigint())
    status: str = Field(default="waiting_capacity", max_length=32)
    s3_upload_id: str | None = Field(default=None, repr=False)
    part_size: int = Field(default=16 * 1024 * 1024, sa_column=bigint())
    parts: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    sha256: str | None = Field(default=None, max_length=64)
    video_md5: str | None = Field(default=None, max_length=32)
    digest_verified_at: datetime | None = Field(default=None, sa_column=timestamp())
    digest_source: str | None = Field(default=None, max_length=64)
    revision: int = 0
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(default=None, sa_column=timestamp())
    reserved_at: datetime | None = Field(default=None, sa_column=timestamp())
    reservation_released_at: datetime | None = Field(
        default=None, sa_column=timestamp()
    )
    received_at: datetime | None = Field(default=None, sa_column=timestamp())
    deleted_at: datetime | None = Field(default=None, sa_column=timestamp())
    expires_at: datetime | None = Field(default=None, sa_column=timestamp())
    error_code: str | None = Field(default=None, max_length=128)
    next_attempt_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )

    @property
    def original_available(self) -> bool:
        return self.status == "verified"


class OriginalUse(SQLModel, table=True):
    __tablename__ = "original_use"
    __table_args__ = (
        object_reference(),
        ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["tenant_membership.tenant_id", "tenant_membership.user_id"],
        ),
        CheckConstraint(
            "revision >= 0 AND status IN ('active','released','expired')",
            name="ck_original_use_status",
        ),
        Index(
            "ix_original_use_object",
            "tenant_id",
            "bc_id",
            "material_id",
            "generation",
            "status",
            "expires_at",
        ),
        Index("ix_original_use_expiry", "status", "expires_at", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    material_id: UUID
    generation: int
    actor_id: UUID
    purpose: str = Field(max_length=64)
    operation_id: UUID | None = None
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    nonce: UUID = Field(default_factory=uuid4, unique=True)
    status: str = Field(default="active", max_length=16)
    revision: int = 0
    completion_evidence: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    expires_at: datetime = Field(sa_column=timestamp(nullable=False))
    permission_issued_at: datetime | None = Field(default=None, sa_column=timestamp())
    released_at: datetime | None = Field(default=None, sa_column=timestamp())
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )


class ObjectCleanup(SQLModel, table=True):
    __tablename__ = "object_cleanup"
    __table_args__ = (
        object_reference(),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "material_id",
            "generation",
            name="uq_object_cleanup_generation",
        ),
        CheckConstraint(
            "status IN ('pending','deleting','delete_unknown','deleted','blocked') AND revision >= 0 AND attempt_count >= 0",
            name="ck_object_cleanup_status",
        ),
        CheckConstraint(
            "(claim_token IS NULL) = (claimed_until IS NULL)",
            name="ck_object_cleanup_claim",
        ),
        Index("ix_object_cleanup_recovery", "status", "next_attempt_at", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    material_id: UUID
    generation: int
    reason: str = Field(max_length=128)
    eligibility_evidence: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    status: str = Field(default="pending", max_length=32)
    revision: int = 0
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(default=None, sa_column=timestamp())
    dispatch_id: UUID | None = Field(default=None, foreign_key="pending_dispatch.id")
    attempt_count: int = 0
    delete_sent_at: datetime | None = Field(default=None, sa_column=timestamp())
    abort_confirmed_at: datetime | None = Field(default=None, sa_column=timestamp())
    delete_confirmed_at: datetime | None = Field(default=None, sa_column=timestamp())
    head_confirmed_at: datetime | None = Field(default=None, sa_column=timestamp())
    error_code: str | None = Field(default=None, max_length=128)
    next_attempt_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )


class ObjectBudget(SQLModel, table=True):
    __tablename__ = "object_budget"
    __table_args__ = (
        CheckConstraint(
            "(scope_key = 'global' AND tenant_id IS NULL) OR (tenant_id IS NOT NULL AND scope_key = 'tenant:' || tenant_id::text)",
            name="ck_object_budget_scope",
        ),
        CheckConstraint(
            "reserved_bytes >= 0 AND stored_bytes >= 0 AND stored_bytes <= reserved_bytes AND revision >= 0",
            name="ck_object_budget_numbers",
        ),
    )
    scope_key: str = Field(primary_key=True, max_length=64)
    tenant_id: UUID | None = Field(default=None, foreign_key="tenant.id")
    reserved_bytes: int = Field(default=0, sa_column=bigint())
    stored_bytes: int = Field(default=0, sa_column=bigint())
    revision: int = 0


class SourceAccountLoad(SQLModel, table=True):
    __tablename__ = "source_account_load"
    __table_args__ = (
        access_reference(),
        CheckConstraint(
            "in_flight >= 0 AND revision >= 0", name="ck_source_load_numbers"
        ),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    connection_id: UUID = Field(primary_key=True)
    in_flight: int = 0
    revision: int = 0
    last_assigned_at: datetime | None = Field(default=None, sa_column=timestamp())
    cooldown_until: datetime | None = Field(default=None, sa_column=timestamp())


class IngestMilestone(SQLModel, table=True):
    __tablename__ = "ingest_milestone"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "session_id", "material_id"],
            [
                "ingest_session_file.tenant_id",
                "ingest_session_file.bc_id",
                "ingest_session_file.session_id",
                "ingest_session_file.material_id",
            ],
        ),
        UniqueConstraint(
            "session_id", "material_id", "milestone", name="uq_ingest_milestone"
        ),
        CheckConstraint(
            "milestone IN ('accepted','uploaded','ready','cleaned') AND byte_size > 0",
            name="ck_ingest_milestone",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    session_id: UUID
    material_id: UUID
    milestone: str = Field(max_length=16)
    byte_size: int = Field(sa_column=bigint())
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )


class IngestTransition(SQLModel, table=True):
    __tablename__ = "ingest_transition"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "file_id"],
            ["ingest_session_file.tenant_id", "ingest_session_file.id"],
        ),
        UniqueConstraint("file_id", "revision", name="uq_ingest_transition_revision"),
        CheckConstraint(
            "revision > 0 AND generation > 0", name="ck_ingest_transition_numbers"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    file_id: UUID
    generation: int
    revision: int
    from_status: str = Field(max_length=32)
    to_status: str = Field(max_length=32)
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp(nullable=False)
    )


def record_milestone(
    db: Session,
    *,
    tenant_id: UUID,
    bc_id: str,
    session_id: UUID,
    material_id: UUID,
    milestone: str,
) -> bool:
    """Record a lifetime file fact once, using fixed-size indexed SQL only."""
    if milestone not in {"accepted", "uploaded", "ready", "cleaned"}:
        raise ValueError("unknown ingest milestone")
    size = db.exec(
        select(col(IngestSessionFile.byte_size)).where(
            col(IngestSessionFile.tenant_id) == tenant_id,
            col(IngestSessionFile.bc_id) == bc_id,
            col(IngestSessionFile.session_id) == session_id,
            col(IngestSessionFile.material_id) == material_id,
        )
    ).one()
    inserted = db.exec(
        insert(IngestMilestone)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            bc_id=bc_id,
            session_id=session_id,
            material_id=material_id,
            milestone=milestone,
            byte_size=size,
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(constraint="uq_ingest_milestone")
        .returning(col(IngestMilestone.id))
    ).first()
    if inserted is None:
        return False
    count_column = getattr(IngestSession, f"{milestone}_count")
    byte_column = getattr(IngestSession, f"{milestone}_bytes")
    db.exec(
        update(IngestSession)
        .where(
            col(IngestSession.id) == session_id,
            col(IngestSession.tenant_id) == tenant_id,
            col(IngestSession.bc_id) == bc_id,
        )
        .values({count_column: count_column + 1, byte_column: byte_column + size})
    )
    return True


def transition_ingest_file(
    db: Session,
    *,
    tenant_id: UUID,
    file_id: UUID,
    expected_revision: int,
    status: str,
    error_code: str | None = None,
) -> bool:
    """CAS current state and failed count; late/repeated revisions have no effect.

    This does not assert provider evidence or authorize a transition. The caller
    owns that policy and records cumulative milestones after verified outcomes.
    """
    row = db.exec(
        select(IngestSessionFile)
        .where(
            col(IngestSessionFile.tenant_id) == tenant_id,
            col(IngestSessionFile.id) == file_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None or row.revision != expected_revision:
        return False
    previous = row.status
    if previous == status:
        return False
    row.status = status
    row.revision += 1
    row.error_code = error_code
    db.add(
        IngestTransition(
            tenant_id=tenant_id,
            file_id=file_id,
            generation=row.current_generation,
            revision=row.revision,
            from_status=previous,
            to_status=status,
        )
    )
    db.flush()
    delta = int(status == "failed") - int(previous == "failed")
    if delta:
        db.exec(
            update(IngestSession)
            .where(
                col(IngestSession.tenant_id) == tenant_id,
                col(IngestSession.id) == row.session_id,
            )
            .values(failed_count=col(IngestSession.failed_count) + delta)
        )
    return True
