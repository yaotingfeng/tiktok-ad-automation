from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.providers.schemas import DramaCandidate

InputText = Annotated[str, Field(max_length=1000)]


class ManualLinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_no: int = Field(ge=1, le=1000, strict=True)
    url: str = Field(min_length=1, max_length=8192)
    # 用户提供的版权方编号，可按展示 ID 关联已有剧目；不要求用户填写技术 ID。
    external_drama_id: str = Field(default="", max_length=255)
    protected_base: str = Field(default="", max_length=1000)


class ManualLinkPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: int = Field(gt=0, strict=True)
    link: ManualLinkInput


class CreateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    bc_id: str = Field(min_length=1, max_length=128)
    strategy_version_id: UUID
    provider_connection_id: UUID | None = None
    execution_connection_id: UUID | None = None
    application_id: str | None = Field(default=None, min_length=1, max_length=255)
    custom_provider_name: str | None = Field(default=None, min_length=1, max_length=100)
    manual_links: list[ManualLinkInput] = Field(default_factory=list, max_length=1000)
    drama_lines: list[InputText] = Field(max_length=1000)
    account_lines: list[InputText] = Field(max_length=100_000)
    link_config: dict[str, Any] = Field(default_factory=dict)


class PatchDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID | None = None
    expected_revision: int = Field(gt=0, strict=True)
    strategy_version_id: UUID | None = None
    provider_connection_id: UUID | None = None
    execution_connection_id: UUID | None = None
    application_id: str | None = Field(default=None, min_length=1, max_length=255)
    custom_provider_name: str | None = Field(default=None, min_length=1, max_length=100)
    manual_links: list[ManualLinkInput] | None = Field(default=None, max_length=1000)
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
    request_id: UUID | None = None
    expected_revision: int = Field(gt=0, strict=True)
    groups: list[list[UUID]] = Field(max_length=100_000)


class DraftSummary(BaseModel):
    draft_id: UUID
    revision: int
    bc_id: str
    status: Literal["DRAFT", "PREPARING", "READY", "BLOCKED"]
    strategy_version_id: UUID
    provider_connection_id: UUID
    execution_connection_id: UUID | None = None
    application_id: str
    link_config: dict[str, str | int | bool | None]
    custom_provider_name: str | None = None
    provider_kind: str = ""
    input_counts: dict[str, dict[str, int]]
    drama_count: int
    account_count: int
    task_id: UUID | None
    provider_task_id: UUID | None
    error_code: str | None
    preparation_phase: Literal["accounts", "links", "materials", "done"] | None = None
    created_at: datetime
    updated_at: datetime


class DraftListItem(BaseModel):
    draft_id: UUID
    bc_id: str
    revision: int
    status: Literal["DRAFT", "PREPARING", "READY", "BLOCKED"]
    drama_titles: list[str]
    drama_input_count: int
    account_input_count: int
    resolved_account_count: int
    strategy_label: str
    preview_id: UUID | None
    preview_status: Literal["BUILDING", "FROZEN", "OBSOLETE", "FAILED"] | None
    updated_at: datetime


class DraftDramaPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    drama_id: UUID
    link_id: UUID
    title: str
    first_line: int
    material_state: str
    matched_count: int


class DraftInputPreparation(BaseModel):
    """按输入行展示准备进度；未取得链接时不伪造可搭建剧目。"""

    link_status: str
    external_drama_id: str | None = None
    display_drama_id: str | None = None
    url: str | None = None
    protected_base: str | None = None
    provider_input_id: UUID | None = None
    candidates: list[DramaCandidate] = Field(default_factory=list)
    title: str | None = None
    reason_code: str | None = None
    drama: DraftDramaPublic | None = None


class DraftInputPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: str
    line_no: int
    raw_text: str
    manual_link: dict[str, str | int] = Field(default_factory=dict)
    status: str
    reason_code: str | None
    duplicate_of: int | None
    advertiser_id: str | None
    drama_id: UUID | None
    provider_input_id: UUID | None
    candidates: list[dict[str, Any]]
    preparation: DraftInputPreparation | None = None


class DraftMaterialPublic(BaseModel):
    material_id: UUID
    file_name: str
    source_bc_id: str
    content_key: str
    group_no: int
    position: int
    shared_with_other_drama: bool
