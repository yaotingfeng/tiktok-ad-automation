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
