from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.builds.route_schemas import ExecutionRoutePublic

Readiness = Literal["READY", "PREPARING", "BLOCKED"]


class PreviewAccepted(BaseModel):
    preview_id: UUID


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(gt=0, strict=True)


class PreviewSummary(BaseModel):
    execution_route: ExecutionRoutePublic | None = None
    preview_id: UUID
    draft_id: UUID
    draft_revision: int
    bc_id: str
    status: Literal["BUILDING", "FROZEN", "OBSOLETE", "FAILED"]
    currency: str
    campaign_count: int
    adgroup_count: int
    ad_count: int
    blocked_count: int
    preparing_count: int
    input_issue_count: int
    total_unit_count: int
    daily_budget_sum: Decimal
    content_digest: str | None
    error_code: str | None
    created_at: datetime


class PreviewUnit(BaseModel):
    unit_id: UUID
    drama_id: UUID
    title: str
    advertiser_id: str
    currency: str
    budget: Decimal
    campaign_name: str
    readiness: Readiness
    reason_codes: list[str]
    group_count: int
    ad_count: int


class FrozenUnit(BaseModel):
    model_config = ConfigDict(frozen=True)
    unit_id: UUID
    preview_id: UUID
    tenant_id: UUID
    drama_id: UUID
    link_id: UUID
    strategy_version_id: UUID
    bc_id: str
    advertiser_id: str
    connection_id: UUID
    currency: str
    timezone: str
    campaign_name: str
    protected_base: str
    url: str
    budget: Decimal
    target_roas: Decimal
    readiness: Readiness
    reason_codes: tuple[str, ...]
    scene_snapshot: dict[str, Any]


class FrozenAd(BaseModel):
    model_config = ConfigDict(frozen=True)
    ad_id: UUID
    creative_no: int
    name: str
    copy_id: UUID
    text: str
    cta_option_ids: tuple[str, ...]


class FrozenGroup(BaseModel):
    model_config = ConfigDict(frozen=True)
    group_id: UUID
    group_no: int
    name: str
    material_ids: tuple[UUID, ...]
    ads: tuple[FrozenAd, ...]


class PreviewInputPublic(BaseModel):
    kind: str
    line_no: int
    raw_text: str
    status: str
    reason_code: str | None
    duplicate_of: int | None


class PreviewDramaPublic(BaseModel):
    drama_id: UUID
    title: str
    account_count: int
    ready_count: int
    preparing_count: int
    blocked_count: int
    material_count: int
    material_group_count: int
    eligible_campaign_count: int
    eligible_adgroup_count: int
    eligible_ad_count: int
    daily_budget_sum: Decimal
