"""Small execution contracts; credentials and raw evidence are never public DTOs."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.builds.preview_schemas import FrozenUnit
from app.modules.builds.route_schemas import ExecutionRoutePublic


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class SubmissionReceipt(BaseModel):
    submission_id: UUID
    status: str


class ObjectCounts(BaseModel):
    campaign_count: int = 0
    adgroup_count: int = 0
    ad_count: int = 0


class Recovery(BaseModel):
    retryable_step_count: int = 0
    reconcilable_step_count: int = 0
    can_retry: bool = False
    can_reconcile: bool = False
    reasons: list[str] = Field(default_factory=list)


class SubmissionView(BaseModel):
    execution_route: ExecutionRoutePublic | None = None
    recovery_mode: Literal["ORIGINAL_READ", "REAUTHORIZE_READ", "BLOCKED"] = "BLOCKED"
    error_code: str | None = None
    actor_name: str = ""
    provider_name: str | None = None
    strategy_label: str = ""
    submission_id: UUID
    preview_id: UUID
    draft_id: UUID
    batch_short_id: str
    bc_id: str
    status: str
    expanded: bool
    currency: str
    daily_budget_sum: str
    planned: ObjectCounts
    submitted: ObjectCounts
    succeeded: ObjectCounts
    failed: ObjectCounts
    excluded: ObjectCounts
    unknown: ObjectCounts
    pending: ObjectCounts
    stage_counts: dict[str, int]
    excluded_unit_count: int
    drama_count: int
    account_count: int
    created_at: datetime
    updated_at: datetime
    recovery: Recovery = Field(default_factory=Recovery)


class StepClaim(BaseModel):
    model_config = ConfigDict(frozen=True)
    step_id: UUID
    tenant_id: UUID
    submission_id: UUID
    preview_id: UUID
    unit_id: UUID
    actor_id: UUID
    bc_id: str
    advertiser_id: str
    kind: str
    group_id: UUID | None
    planned_ad_id: UUID | None
    material_id: UUID | None
    parent_step_id: UUID | None
    lease_token: UUID
    lease_expires_at: datetime
    attempt: int
    attempt_id: UUID
    route: FrozenTikTokRoute
    dispatch_revision: int


class ExecutionUnit(BaseModel):
    model_config = ConfigDict(frozen=True)
    submission_id: UUID
    actor_id: UUID
    frozen: FrozenUnit


class StepPublic(BaseModel):
    can_historical_read: bool = False
    title: str | None = None
    advertiser_id: str | None = None
    group_no: int | None = None
    creative_no: int | None = None
    step_id: UUID
    unit_id: UUID
    kind: str
    group_id: UUID | None
    planned_ad_id: UUID | None
    material_id: UUID | None
    status: str
    remote_id: str | None
    error_code: str | None
    operation_status: str | None
    review_status: str | None
    mismatch: bool
    checked_at: datetime | None


class SubmissionUnitPublic(BaseModel):
    result_status: str | None = None
    account_name: str | None = None
    group_count: int = 0
    ad_count: int = 0
    succeeded_group_count: int = 0
    succeeded_ad_count: int = 0
    material_count: int = 0
    ready_material_count: int = 0
    campaign_step: StepPublic | None = None
    unit_id: UUID
    drama_id: UUID
    title: str
    advertiser_id: str
    campaign_name: str
    disposition: str
    reason_codes: list[str]
    expanded: bool


class EvidencePublic(BaseModel):
    evidence_id: UUID
    step_id: UUID
    attempt: int
    conclusion: str
    observed_at: datetime


class SubmissionMetadata(BaseModel):
    actor_name: str
    provider_name: str | None
    strategy_label: str


class SubmissionListItem(SubmissionMetadata):
    submission_id: UUID
    batch_short_id: str
    bc_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    drama_count: int
    account_count: int
    excluded_unit_count: int
    submitted: ObjectCounts
    succeeded: ObjectCounts
    failed: ObjectCounts
    unknown: ObjectCounts


class SubmissionGroupPublic(BaseModel):
    group_id: UUID
    unit_id: UUID
    group_no: int
    name: str
    material_count: int
    ad_count: int
    step: StepPublic | None


class SubmissionAdPublic(BaseModel):
    planned_ad_id: UUID
    group_id: UUID
    creative_no: int
    name: str
    text: str
    cta_option_ids: list[str]
    step: StepPublic | None


class SubmissionEventPublic(EvidencePublic):
    unit_id: UUID
    kind: str


class SubmissionMaterialPublic(BaseModel):
    material_id: UUID
    position: int
    file_name: str
    preview_available: bool
    video_id: str | None
    image_id: str | None
    step: StepPublic | None
