"""广告意图与回读业务合同；不包含 SDK 类型、凭据或可任意扩展的参数。"""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .common import CallEvidence


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("identifier must not be blank")
    return value


def _money(value: object) -> Decimal:
    # 禁止 bool 和二进制浮点；旧 JSON 金额在解码边界显式转成十进制文本。
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("money requires an exact decimal")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError("money requires an exact decimal") from None


def _schedule(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
        raise ValueError("schedule must use UTC YYYY-MM-DD HH:MM:SS")
    datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return value


Id = Annotated[str, Field(strict=True, min_length=1), AfterValidator(_nonblank)]
Money = Annotated[Decimal, BeforeValidator(_money), Field(gt=0, allow_inf_nan=False)]
BuildKind = Literal["CTA", "CAMPAIGN", "ADGROUP", "AD"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CampaignCreate(FrozenModel):
    kind: Literal["CAMPAIGN"] = "CAMPAIGN"
    advertiser_id: Id
    name: Id
    budget: Money
    operation_status: Literal["ENABLE"] = "ENABLE"
    objective_type: Literal["APP_PROMOTION"] = "APP_PROMOTION"
    app_promotion_type: Literal["MINIS"] = "MINIS"
    campaign_type: Literal["REGULAR_CAMPAIGN"] = "REGULAR_CAMPAIGN"
    catalog_enabled: Literal[False] = False
    budget_mode: Literal["BUDGET_MODE_DYNAMIC_DAILY_BUDGET"] = (
        "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"
    )
    budget_optimize_on: Literal[True] = True

    @field_validator("catalog_enabled", "budget_optimize_on", mode="before")
    @classmethod
    def strict_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("boolean setting requires a boolean")
        return value


class AdGroupObservedFacts(FrozenModel):
    kind: Literal["ADGROUP"] = "ADGROUP"
    advertiser_id: Id
    campaign_id: Id
    name: Id
    minis_id: Id
    roas_bid: Money
    location_ids: tuple[Id, ...] = Field(min_length=1)
    schedule_start_time: Annotated[Id, AfterValidator(_schedule)]
    promotion_type: Literal["MINI_APP"] = "MINI_APP"
    optimization_goal: Literal["VALUE"] = "VALUE"
    optimization_event: Literal["ACTIVE_PAY", "IMPRESSION_LEVEL_AD_REVENUE"] = (
        "ACTIVE_PAY"
    )
    bid_type: Literal["BID_TYPE_NO_BID"] = "BID_TYPE_NO_BID"
    deep_bid_type: Literal["VO_MIN_ROAS"] = "VO_MIN_ROAS"
    billing_event: Literal["OCPM"] = "OCPM"
    placement_type: Literal["PLACEMENT_TYPE_NORMAL"] = "PLACEMENT_TYPE_NORMAL"
    placements: tuple[Literal["PLACEMENT_TIKTOK"], ...] = Field(
        default=("PLACEMENT_TIKTOK",), min_length=1
    )
    schedule_type: Literal["SCHEDULE_FROM_NOW"] = "SCHEDULE_FROM_NOW"


class AdGroupCreate(AdGroupObservedFacts):
    operation_status: Literal["ENABLE"] = "ENABLE"


class CreativeAsset(FrozenModel):
    video_id: Id
    image_id: Id


class IdentityFields(FrozenModel):
    identity_id: Id
    identity_type: Literal["BC_AUTH_TT", "TT_USER"]
    identity_authorized_bc_id: Id | None = None

    @model_validator(mode="after")
    def validate_identity_scope(self) -> Self:
        # BC 授权身份必须带实际 BC；账户自有 TT_USER 不伪造 BC 授权字段。
        if (self.identity_type == "BC_AUTH_TT") != (
            self.identity_authorized_bc_id is not None
        ):
            raise ValueError("identity scope does not match identity type")
        return self


class AdCreate(IdentityFields):
    kind: Literal["AD"] = "AD"
    advertiser_id: Id
    adgroup_id: Id
    name: Id
    text: Id = Field(max_length=100)
    landing_page_url: Id
    portfolio_id: Id
    assets: tuple[CreativeAsset, ...] = Field(min_length=1, max_length=50)
    operation_status: Literal["ENABLE"] = "ENABLE"


class CtaAsset(FrozenModel):
    asset_ids: tuple[Id, ...] = Field(min_length=1, max_length=50)
    asset_content: Id


class CtaCreate(FrozenModel):
    kind: Literal["CTA"] = "CTA"
    advertiser_id: Id
    assets: tuple[CtaAsset, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def bounded_selection(self) -> Self:
        if (
            len({identity for asset in self.assets for identity in asset.asset_ids})
            > 50
        ):
            raise ValueError("CTA selection exceeds 50 distinct IDs")
        return self


CreateIntent = Annotated[
    CampaignCreate | AdGroupCreate | AdCreate | CtaCreate, Field(discriminator="kind")
]


class CreatedObject(FrozenModel):
    kind: BuildKind
    remote_id: Id
    operation_status: Id | None
    evidence: CallEvidence


class BuildRecord(FrozenModel):
    remote_id: Id
    intent: CreateIntent | None
    operation_status: Id | None
    missing_fields: tuple[Id, ...]
    observed_adgroup: AdGroupObservedFacts | None = None
    review_status: Id | None = None


class BuildReadQuery(FrozenModel):
    intent: CreateIntent
    remote_id: Id | None = None
    page: int = Field(default=1, strict=True, ge=1, le=1000)


class BuildPage(FrozenModel):
    rows: tuple[BuildRecord, ...]
    page: int = Field(strict=True, ge=1, le=1000)
    total_pages: int = Field(strict=True, ge=0, le=1000)
    total_number: int = Field(strict=True, ge=0)
    complete: bool = Field(strict=True)
    evidence: CallEvidence


class AdGroupStatus(FrozenModel):
    advertiser_id: Id
    adgroup_id: Id
    operation_status: Id | None
    evidence: CallEvidence
    review_status: Id | None = None


class BuildOperations(Protocol):
    def create(self, *, attempt_id: UUID, intent: CreateIntent) -> CreatedObject: ...
    def read_page(self, *, query: BuildReadQuery) -> BuildPage: ...
    def read_adgroup_status(
        self, *, advertiser_id: str, adgroup_id: str
    ) -> AdGroupStatus: ...
