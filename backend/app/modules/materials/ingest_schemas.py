"""Public, bounded ingest contracts. Signed URLs exist only in response DTOs."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from .schemas import UploadFileRequest


class IngestSessionCreate(BaseModel):
    model_config = {"extra": "forbid"}
    bc_id: str = Field(min_length=1, max_length=128)
    request_id: UUID
    file_count: int = Field(ge=1, le=20_000, strict=True)
    total_bytes: int = Field(gt=0, le=20_000 * 5 * 1024**4, strict=True)


class IngestFileInput(UploadFileRequest):
    client_index: int = Field(ge=0, lt=20_000, strict=True)
    last_modified_ms: int | None = Field(default=None, ge=0, strict=True)


class IngestChunkCreate(BaseModel):
    model_config = {"extra": "forbid"}
    request_id: UUID
    files: list[IngestFileInput] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_indexes(self) -> Self:
        indexes = [item.client_index for item in self.files]
        if len(set(indexes)) != len(indexes):
            raise ValueError("client_index must be unique within a chunk")
        return self


class IngestSummary(BaseModel):
    session_id: UUID
    bc_id: str
    expected_count: int
    total_bytes: int
    status: str
    registration_cursor: int
    accepted_count: int = Field(description="Cumulative distinct accepted files.")
    uploaded_count: int = Field(
        description="Cumulative files completely received at least once."
    )
    ready_count: int = Field(
        description="Cumulative files verified in their actual source account."
    )
    failed_count: int = Field(
        description="Current failed files; decreases when retried."
    )
    cleaned_count: int = Field(
        description="Cumulative files with a confirmed original cleanup."
    )
    reserved_bytes: int = Field(
        description="Current session occupancy, including unconfirmed deletion."
    )
    stored_bytes: int = Field(
        description="Current stored bytes, a subset of reserved bytes."
    )
    next_cursor: str | None = None
    created_at: datetime


class IngestFilePublic(BaseModel):
    material_id: UUID
    client_index: int
    file_name: str
    size: int
    mime_type: str
    last_modified_ms: int | None = None
    generation: int
    upload_id: str | None
    part_size: int
    part_count: int
    can_retry: bool
    operation_revision: int
    platform_status: str
    temporary_storage_status: str
    received_bytes: int
    source_advertiser_id: str | None
    error_code: str | None
    operation_status: str = "idle"
    task_id: UUID | None = None


class IngestChunkResult(BaseModel):
    session_id: UUID
    request_id: UUID
    items: list[IngestFilePublic]


class IngestIdentity(BaseModel):
    model_config = {"extra": "forbid"}
    generation: int = Field(ge=1, strict=True)
    upload_id: str | None = Field(default=None, max_length=512)
    operation_revision: int = Field(ge=0, strict=True)


class IngestPartUrlsCreate(IngestIdentity):
    request_id: UUID = Field(default_factory=uuid4)
    upload_id: str = Field(min_length=1, max_length=512)
    part_numbers: list[int] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def bounded_parts(self) -> Self:
        if any(
            type(number) is not int or not 1 <= number <= 10_000
            for number in self.part_numbers
        ):
            raise ValueError("part numbers must be bounded integers")
        if len(set(self.part_numbers)) != len(self.part_numbers):
            raise ValueError("part numbers must be distinct")
        return self


class IngestPartUrl(BaseModel):
    part_number: int
    byte_size: int
    url: str = Field(repr=False)
    expires_in: int
    permission_id: UUID
    permission_nonce: UUID
    permission_revision: int


class IngestPartPermission(BaseModel):
    part_number: int
    permission_id: UUID
    permission_nonce: UUID
    permission_revision: int
    outcome: Literal["signed", "completed", "unused", "unknown"]


class IngestPartPermissions(BaseModel):
    generation: int
    upload_id: str
    operation_revision: int
    request_id: UUID
    items: list[IngestPartPermission]


class IngestPartReceipt(BaseModel):
    model_config = {"extra": "forbid"}
    part_number: int = Field(ge=1, le=10000, strict=True)
    permission_id: UUID
    permission_nonce: UUID
    permission_revision: int = Field(ge=0, strict=True)
    outcome: Literal["completed", "unused", "unknown"]
    etag: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def completed_etag(self) -> Self:
        if self.outcome == "completed" and not self.etag:
            raise ValueError("completed PUT requires an ETag")
        return self


class IngestPartReceiptsCreate(IngestIdentity):
    receipts: list[IngestPartReceipt] = Field(min_length=1, max_length=2)


class IngestPartReceiptsResult(BaseModel):
    generation: int
    upload_id: str
    operation_revision: int
    accepted_permission_ids: list[UUID]


class IngestPartUrls(BaseModel):
    generation: int
    upload_id: str
    operation_revision: int
    items: list[IngestPartUrl]


class IngestPart(BaseModel):
    part_number: int
    byte_size: int
    etag: str


class IngestPartsPage(BaseModel):
    generation: int
    upload_id: str
    operation_revision: int
    items: list[IngestPart]
    next_cursor: str | None = None


class IngestSealIssue(BaseModel):
    code: Literal["file_count_mismatch", "total_bytes_mismatch"]
    expected: int
    accepted: int


class IngestSealResult(BaseModel):
    sealed: bool
    issues: list[IngestSealIssue]
    summary: IngestSummary
