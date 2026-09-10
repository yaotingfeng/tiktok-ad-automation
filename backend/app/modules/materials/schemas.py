from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class AccountAsset(BaseModel):
    asset_id: UUID
    material_id: UUID
    bc_id: str
    advertiser_id: str
    connection_id: UUID
    video_id: str
    mid: str | None = None
    image_id: str | None = None
    cover_url: str | None = None
    status: Literal["available", "unavailable", "result_unknown"]
    verified_at: datetime | None = None

    @model_validator(mode="after")
    def available_requires_verified_identity(self) -> Self:
        if self.status == "available" and (
            not self.video_id.strip() or not self.verified_at
        ):
            raise ValueError("available asset requires a verified video identity")
        return self


class MaterialCandidate(BaseModel):
    material_id: UUID
    file_name: str
    bc_id: str
    original_available: bool
    source_assets: list[AccountAsset] = Field(
        default_factory=list,
        description="At most one representative verified location; use paged asset details for all locations.",
    )


class MaterialReadiness(BaseModel):
    state: Literal["ready", "preparable", "blocked"]
    path: Literal["existing_target", "share_source", "upload_original", "unavailable"]
    mapping: AccountAsset | None = None
    reason_code: str | None = None
    reason_message: str | None = None

    @model_validator(mode="after")
    def ready_requires_mapping(self) -> Self:
        if self.state == "ready" and (
            not self.mapping
            or self.mapping.status != "available"
            or self.path != "existing_target"
        ):
            raise ValueError("ready requires an available target mapping")
        return self


class AssetPreparation(BaseModel):
    state: Literal["ready", "queued", "blocked"]
    mapping: AccountAsset | None = None
    task_id: UUID | None = None
    reason_code: str | None = None
    reason_message: str | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.state == "ready" and (
            not self.mapping or self.mapping.status != "available"
        ):
            raise ValueError("ready requires an available target mapping")
        if self.state == "queued" and self.task_id is None:
            raise ValueError("queued requires a durable task ID")
        return self


VIDEO_MIME_TYPES = frozenset(
    {
        "video/mp4",
        "video/quicktime",
        "video/x-msvideo",
        "video/webm",
        "video/mpeg",
        "video/x-matroska",
    }
)


class UploadFileRequest(BaseModel):
    model_config = {"extra": "forbid"}

    file_name: str = Field(min_length=1, max_length=1000)
    size: int = Field(gt=0, le=5 * 1024**4, strict=True)
    mime_type: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid_file(self) -> Self:
        if not self.file_name.strip() or any(ord(char) < 32 for char in self.file_name):
            raise ValueError("文件名无效")
        if self.mime_type not in VIDEO_MIME_TYPES:
            raise ValueError("请选择视频文件")
        return self


class UploadedPart(BaseModel):
    model_config = {"extra": "forbid"}
    part_number: int = Field(ge=1, le=10000, strict=True)
    etag: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def valid_etag(self) -> Self:
        if not self.etag.strip() or any(ord(char) < 32 for char in self.etag):
            raise ValueError("分片校验值无效")
        return self


UploadStage = Literal[
    "receiving",
    "stored",
    "uploading",
    "verifying",
    "available",
    "blocked",
    "result_unknown",
]


class UploadFileResult(BaseModel):
    material_id: UUID
    upload_id: UUID
    file_name: str
    byte_size: int
    part_size: int
    part_count: int
    status: UploadStage
    received_bytes: int | None = None
    task_id: UUID | None = None
    latest_advertiser_id: str | None = None
    can_retry: bool = False
    error_code: str | None = None


class UploadBatchResult(BaseModel):
    batch_id: UUID
    bc_id: str
    status: UploadStage
    files: list[UploadFileResult]


class UploadBatchRequest(BaseModel):
    model_config = {"extra": "forbid"}
    bc_id: str = Field(min_length=1, max_length=128)
    request_id: UUID
    files: list[UploadFileRequest] = Field(min_length=1, max_length=200)


class CompleteUploadRequest(BaseModel):
    model_config = {"extra": "forbid"}
    parts: list[UploadedPart] = Field(min_length=1, max_length=10000)


class SignedPart(BaseModel):
    url: str = Field(repr=False)
    expires_in: int = 900


class UploadCompleted(BaseModel):
    task_id: UUID


class MaterialPublic(BaseModel):
    material_id: UUID
    bc_id: str
    file_name: str
    byte_size: int
    mime_type: str
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    created_at: datetime
    original_available: bool
    status: UploadStage
    available_account_count: int
    latest_advertiser_id: str | None = None


class UploadAttemptPublic(BaseModel):
    attempt_id: UUID
    material_id: UUID
    advertiser_id: str
    connection_id: UUID
    status: str
    created_at: datetime
    error_code: str | None = None


class UploadBatchSummary(BaseModel):
    batch_id: UUID
    bc_id: str
    status: UploadStage
    file_count: int
    created_at: datetime


class SignedPreview(BaseModel):
    url: str = Field(repr=False)
    expires_in: int = 300


class RemoteMaterialPreview(BaseModel):
    """Current response-only URL; platform expiry is not assumed."""

    url: str = Field(repr=False)
    advertiser_id: str
    video_id: str
    width: int
    height: int
    duration: float
    format: str
