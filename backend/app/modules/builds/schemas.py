from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

InputText = Annotated[str, Field(max_length=1000)]


class CreateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    bc_id: str = Field(min_length=1, max_length=128)
    strategy_version_id: UUID
    provider_connection_id: UUID
    application_id: str = Field(min_length=1, max_length=255)
    drama_lines: list[InputText] = Field(max_length=1000)
    account_lines: list[InputText] = Field(max_length=100_000)
    link_config: dict[str, Any] = Field(default_factory=dict)


class PatchDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(gt=0, strict=True)
    strategy_version_id: UUID | None = None
    provider_connection_id: UUID | None = None
    application_id: str | None = Field(default=None, min_length=1, max_length=255)
    drama_lines: list[InputText] | None = Field(default=None, max_length=1000)
    account_lines: list[InputText] | None = Field(default=None, max_length=100_000)
    link_config: dict[str, Any] | None = None


class DraftSaved(BaseModel):
    draft_id: UUID
    revision: int


class DraftPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class DraftPrepareAccepted(BaseModel):
    task_id: UUID
    revision: int


class DraftGroupEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(gt=0, strict=True)
    groups: list[list[UUID]] = Field(max_length=100_000)


class DraftSummary(BaseModel):
    draft_id: UUID
    revision: int
    bc_id: str
    status: Literal["DRAFT", "PREPARING", "READY", "BLOCKED"]
    strategy_version_id: UUID
    provider_connection_id: UUID
    application_id: str
    link_config: dict[str, str | int | bool | None]
    input_counts: dict[str, dict[str, int]]
    drama_count: int
    account_count: int
    task_id: UUID | None
    provider_task_id: UUID | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class DraftInputPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: str
    line_no: int
    raw_text: str
    status: str
    reason_code: str | None
    duplicate_of: int | None
    advertiser_id: str | None
    drama_id: UUID | None
    provider_input_id: UUID | None
    candidates: list[dict[str, Any]]


class DraftDramaPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    drama_id: UUID
    link_id: UUID
    title: str
    first_line: int
    material_state: str
    matched_count: int


class DraftMaterialPublic(BaseModel):
    material_id: UUID
    file_name: str
    group_no: int
    position: int
    shared_with_other_drama: bool
