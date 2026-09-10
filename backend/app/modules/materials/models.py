from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    event,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import object_session
from sqlmodel import Field, SQLModel

if TYPE_CHECKING:
    from .ingest_models import TemporaryMaterialObject

# Python str.strip whitespace, expressed with SQL chr() to avoid dependence on
# database locale and to include nonbreaking and ideographic spaces.
_VIDEO_ID_SPACES = (
    *range(9, 14),
    *range(28, 33),
    133,
    160,
    5760,
    *range(8192, 8203),
    8232,
    8233,
    8239,
    8287,
    12288,
)
VIDEO_ID_NONEMPTY = (
    "length(translate(video_id, "
    + " || ".join(f"chr({value})" for value in _VIDEO_ID_SPACES)
    + ", '')) > 0"
)


def material_reference() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "bc_id", "material_id"],
        ["material_file.tenant_id", "material_file.bc_id", "material_file.id"],
    )


def account_reference() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "advertiser_id"],
        ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"],
    )


def access_reference() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "bc_id", "advertiser_id", "connection_id"],
        [
            "bc_account_access.tenant_id",
            "bc_account_access.bc_id",
            "bc_account_access.advertiser_id",
            "bc_account_access.connection_id",
        ],
    )


class MaterialFile(SQLModel, table=True):
    __tablename__ = "material_file"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_material_tenant_id"),
        UniqueConstraint("tenant_id", "bc_id", "id", name="uq_material_tenant_bc_id"),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        CheckConstraint("byte_size > 0", name="ck_material_size"),
        CheckConstraint(
            "storage_state IN ('receiving','stored','unavailable')",
            name="ck_material_storage",
        ),
        Index(
            "ix_material_tenant_bc_name_id",
            "tenant_id",
            "bc_id",
            "file_name",
            "id",
        ),
        Index(
            "ix_material_folded_trgm",
            "file_name_folded",
            postgresql_using="gin",
            postgresql_ops={"file_name_folded": "gin_trgm_ops"},
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    file_name: str = Field(
        max_length=1000, sa_column=Column(String(1000, collation="C"), nullable=False)
    )
    file_name_folded: str = ""
    object_key: str = Field(unique=True, max_length=512)
    byte_size: int = Field(sa_column=Column(BigInteger, nullable=False))
    sha256: str | None = Field(default=None, max_length=64)
    video_md5: str | None = Field(default=None, max_length=32)
    mime_type: str = Field(default="video/mp4", max_length=128)
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    storage_state: str = "receiving"
    # Null means the legacy upload path; migrated originals have generation 1.
    current_object_generation: int | None = None
    digest_verified_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    digest_source: str | None = Field(default=None, max_length=64)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    @property
    def current_object(self) -> TemporaryMaterialObject | None:
        from .ingest_models import TemporaryMaterialObject

        db = object_session(self)
        if db is None or self.current_object_generation is None:
            return None
        return db.execute(
            select(TemporaryMaterialObject)
            .where(
                TemporaryMaterialObject.tenant_id == self.tenant_id,
                TemporaryMaterialObject.bc_id == self.bc_id,
                TemporaryMaterialObject.material_id == self.id,
                TemporaryMaterialObject.generation == self.current_object_generation,
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    @property
    def original_available(self) -> bool:
        if self.current_object_generation is None:
            return self.storage_state == "stored"
        original = self.current_object
        return original is not None and original.original_available


@event.listens_for(MaterialFile, "before_insert")
@event.listens_for(MaterialFile, "before_update")
def fold_filename(_mapper: object, _connection: object, target: MaterialFile) -> None:
    target.file_name_folded = target.file_name.casefold()


class UploadBatch(SQLModel, table=True):
    __tablename__ = "upload_batch"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_upload_batch_request"),
        UniqueConstraint("tenant_id", "bc_id", "id", name="uq_upload_batch_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    actor_id: UUID = Field(foreign_key="user.id")
    request_id: UUID
    request_digest: str = Field(max_length=64)
    status: str = "receiving"
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ObjectUpload(SQLModel, table=True):
    __tablename__ = "object_upload"
    __table_args__ = (
        material_reference(),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "batch_id"],
            ["upload_batch.tenant_id", "upload_batch.bc_id", "upload_batch.id"],
        ),
        UniqueConstraint("tenant_id", "material_id", name="uq_object_upload_material"),
        CheckConstraint(
            "expected_size > 0 AND part_size > 0", name="ck_object_upload_size"
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    material_id: UUID
    batch_id: UUID
    s3_upload_id: str | None = Field(default=None, repr=False)
    object_key: str = Field(max_length=512)
    expected_size: int = Field(sa_column=Column(BigInteger, nullable=False))
    part_size: int = Field(
        default=16 * 1024 * 1024, sa_column=Column(BigInteger, nullable=False)
    )
    parts: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    status: str = "pending"
    task_id: UUID | None = None
    error_code: str | None = None
    attempt_token: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class MaterialAssetOperation(SQLModel, table=True):
    __tablename__ = "material_asset_operation"
    __table_args__ = (
        material_reference(),
        account_reference(),
        CheckConstraint(
            "status IN ('pending','sending','result_unknown','verifying','confirmed_absent','succeeded','failed')",
            name="ck_material_operation_status",
        ),
        CheckConstraint(
            "path IN ('upload_original','share_source')",
            name="ck_material_operation_path",
        ),
        CheckConstraint(
            "status != 'sending' OR attempt_token IS NOT NULL",
            name="ck_material_operation_claim",
        ),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "material_id",
            "advertiser_id",
            "id",
            name="uq_asset_operation_identity",
        ),
        Index(
            "uq_material_unverified_operation",
            "tenant_id",
            "material_id",
            "advertiser_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','sending','result_unknown','verifying','confirmed_absent')"
            ),
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    material_id: UUID
    advertiser_id: str = Field(max_length=128)
    path: str
    status: str = "pending"
    attempt_token: UUID | None = None
    claimed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    request_digest: str = Field(max_length=64)
    remote_response: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False), repr=False
    )


class MaterialUploadAttempt(SQLModel, table=True):
    __tablename__ = "material_upload_attempt"
    __table_args__ = (
        material_reference(),
        access_reference(),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id", "advertiser_id", "operation_id"],
            [
                "material_asset_operation.tenant_id",
                "material_asset_operation.bc_id",
                "material_asset_operation.material_id",
                "material_asset_operation.advertiser_id",
                "material_asset_operation.id",
            ],
        ),
        Index("ix_upload_attempt_material", "tenant_id", "material_id", "id"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    material_id: UUID
    advertiser_id: str = Field(max_length=128)
    connection_id: UUID
    operation_id: UUID
    status: str = "pending"
    request_digest: str = Field(max_length=64)
    remote_response: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False), repr=False
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AccountMaterial(SQLModel, table=True):
    __tablename__ = "account_material"
    __table_args__ = (
        material_reference(),
        access_reference(),
        UniqueConstraint(
            "tenant_id",
            "material_id",
            "advertiser_id",
            name="uq_account_material_target",
        ),
        UniqueConstraint(
            "tenant_id",
            "bc_id",
            "material_id",
            "id",
            name="uq_account_material_identity",
        ),
        CheckConstraint(
            "status IN ('available','unavailable','result_unknown')",
            name="ck_account_material_status",
        ),
        CheckConstraint(
            f"status != 'available' OR ({VIDEO_ID_NONEMPTY} AND verified_at IS NOT NULL)",
            name="ck_account_material_verified",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    material_id: UUID
    advertiser_id: str = Field(max_length=128)
    connection_id: UUID
    video_id: str
    mid: str | None = None
    image_id: str | None = None
    cover_url: str | None = None
    status: str = "unavailable"
    verified_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class MaterialDistribution(SQLModel, table=True):
    __tablename__ = "material_distribution"
    __table_args__ = (
        material_reference(),
        account_reference(),
        CheckConstraint(
            "status IN ('queued','preparing','verifying','ready','blocked','result_unknown')",
            name="ck_material_distribution_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id", "source_asset_id"],
            [
                "account_material.tenant_id",
                "account_material.bc_id",
                "account_material.material_id",
                "account_material.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id", "advertiser_id", "operation_id"],
            [
                "material_asset_operation.tenant_id",
                "material_asset_operation.bc_id",
                "material_asset_operation.material_id",
                "material_asset_operation.advertiser_id",
                "material_asset_operation.id",
            ],
        ),
        Index(
            "uq_material_pending_distribution",
            "tenant_id",
            "material_id",
            "advertiser_id",
            unique=True,
            postgresql_where=text(
                "status IN ('queued','preparing','verifying','result_unknown')"
            ),
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    material_id: UUID
    advertiser_id: str = Field(max_length=128)
    actor_id: UUID = Field(foreign_key="user.id")
    source_asset_id: UUID | None = None
    operation_id: UUID | None = None
    path: str
    status: str = "queued"
    reason_code: str | None = None
