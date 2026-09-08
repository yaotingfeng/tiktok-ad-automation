"""Small execution contracts; credentials and raw evidence are never public DTOs."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.builds.preview_schemas import FrozenUnit


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
    dispatch_revision: int


class ExecutionUnit(BaseModel):
    model_config = ConfigDict(frozen=True)
    submission_id: UUID
    actor_id: UUID
    frozen: FrozenUnit


class StepPublic(BaseModel):
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
