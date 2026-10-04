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

from app.modules.builds.targeting_schemas import AgeGroup, Gender, Language

from .common import CallEvidence, McpBusinessResponse


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
BudgetStrategy = Literal["SERIES", "ADGROUP"]
BidStrategy = Literal["HIGHEST_VALUE", "TARGET_ROAS"]

# 创建和回读的事件枚举来自不同平台接口，不能用创建值覆盖回读事实。
# 统一竞价策略由 deep_bid_type/roas_bid 推导，事件仅按通道保留为观察字段。
CREATE_HIGHEST_VALUE_EVENT = "AD_REVENUE_VALUE"
STANDARD_READBACK_EVENTS = {
    "HIGHEST_VALUE": "ACTIVE_PAY",
    "TARGET_ROAS": "ACTIVE_PAY",
}
SMART_PLUS_READBACK_EVENTS = {
    "HIGHEST_VALUE": "IMPRESSION_LEVEL_AD_REVENUE",
    "TARGET_ROAS": "IMPRESSION_LEVEL_AD_REVENUE",
}


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CampaignCreate(FrozenModel):
    kind: Literal["CAMPAIGN"] = "CAMPAIGN"
    advertiser_id: Id
    name: Id
    # 组预算时 Campaign 使用平台的无限预算形态；预算值固定在 Ad Group。
    budget: Money | None = None
    budget_strategy: BudgetStrategy = "SERIES"
    operation_status: Literal["ENABLE"] = "ENABLE"
    objective_type: Literal["APP_PROMOTION"] = "APP_PROMOTION"
    app_promotion_type: Literal["MINIS"] = "MINIS"
    campaign_type: Literal["REGULAR_CAMPAIGN"] = "REGULAR_CAMPAIGN"
    budget_mode: Literal[
        "BUDGET_MODE_DYNAMIC_DAILY_BUDGET", "BUDGET_MODE_INFINITE"
    ] = "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"
    budget_optimize_on: Literal[True, False] | None = True

    @model_validator(mode="before")
    @classmethod
    def infer_budget_strategy(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        values = dict(value)
        if "budget_strategy" not in values:
            values["budget_strategy"] = (
                "ADGROUP"
                if values.get("budget_mode") == "BUDGET_MODE_INFINITE"
                else "SERIES"
            )
        if values["budget_strategy"] == "ADGROUP":
            values.setdefault("budget_mode", "BUDGET_MODE_INFINITE")
            values.setdefault("budget_optimize_on", None)
        return values

    @model_validator(mode="after")
    def validate_budget_contract(self) -> Self:
        if self.budget_strategy == "SERIES":
            if self.budget is None:
                raise ValueError("series budget requires campaign budget")
            if self.budget_mode != "BUDGET_MODE_DYNAMIC_DAILY_BUDGET":
                raise ValueError("series budget requires dynamic daily budget mode")
            if self.budget_optimize_on is not True:
                raise ValueError("series budget requires budget optimization")
        else:
            if self.budget is not None:
                raise ValueError("adgroup budget cannot include campaign budget")
            if self.budget_mode != "BUDGET_MODE_INFINITE":
                raise ValueError("adgroup budget requires infinite campaign mode")
            if self.budget_optimize_on not in {None, False}:
                raise ValueError("adgroup budget cannot enable campaign optimization")
        return self

    @field_validator("budget_optimize_on", mode="before")
    @classmethod
    def strict_boolean(cls, value: object) -> object:
        if value is not None and type(value) is not bool:
            raise ValueError("boolean setting requires a boolean")
        return value


class AdGroupObservedFacts(FrozenModel):
    kind: Literal["ADGROUP"] = "ADGROUP"
    advertiser_id: Id
    campaign_id: Id
    name: Id
    minis_id: Id
    vbo_window: Literal["ZERO_DAY"] | None = None
    budget_strategy: BudgetStrategy = "SERIES"
    bid_strategy: BidStrategy = "TARGET_ROAS"
    budget: Money | None = None
    budget_mode: Literal["BUDGET_MODE_DYNAMIC_DAILY_BUDGET"] | None = None
    roas_bid: Money | None = None
    location_ids: tuple[Id, ...] = Field(min_length=1)
    # None 只用于表达历史请求没有该字段，不为新预览补造远端观测值。
    languages: tuple[Language, ...] | None = Field(default=None, min_length=1)
    age_groups: tuple[AgeGroup, ...] | None = Field(default=None, min_length=1)
    gender: Gender | None = None
    targeting_optimization_mode: Literal["MANUAL", "AUTOMATIC"] | None = None

    @field_validator("languages", "age_groups")
    @classmethod
    def canonical_targeting(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        return tuple(sorted(set(value))) if value is not None else None

    @model_validator(mode="after")
    def canonical_manual_regions(self) -> Self:
        if self.targeting_optimization_mode == "MANUAL":
            object.__setattr__(
                self, "location_ids", tuple(sorted(set(self.location_ids)))
            )
        return self

    schedule_start_time: Annotated[Id, AfterValidator(_schedule)]
    promotion_type: Literal["MINI_APP"] = "MINI_APP"
    optimization_goal: Literal["VALUE"] = "VALUE"
    # 创建合同使用 AD_REVENUE_VALUE；历史标准/Smart+ 回读仍可能分别返回
    # ACTIVE_PAY 或 IMPRESSION_LEVEL_AD_REVENUE，三者均保留为观察事实。
    optimization_event: Literal[
        "AD_REVENUE_VALUE", "ACTIVE_PAY", "IMPRESSION_LEVEL_AD_REVENUE"
    ] = "AD_REVENUE_VALUE"
    bid_type: Literal["BID_TYPE_NO_BID"] = "BID_TYPE_NO_BID"
    deep_bid_type: Literal["VO_HIGHEST_VALUE", "VO_MIN_ROAS"] = "VO_MIN_ROAS"
    billing_event: Literal["OCPM"] = "OCPM"
    placement_type: Literal["PLACEMENT_TYPE_NORMAL"] = "PLACEMENT_TYPE_NORMAL"
    placements: tuple[Literal["PLACEMENT_TIKTOK"], ...] = Field(
        default=("PLACEMENT_TIKTOK",), min_length=1
    )
    schedule_type: Literal["SCHEDULE_FROM_NOW"] = "SCHEDULE_FROM_NOW"

    @model_validator(mode="before")
    @classmethod
    def infer_budget_and_bid_strategy(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        values = dict(value)
        if "budget_strategy" not in values:
            values["budget_strategy"] = (
                "ADGROUP" if values.get("budget") is not None else "SERIES"
            )
        if values["budget_strategy"] == "ADGROUP":
            values.setdefault("budget_mode", "BUDGET_MODE_DYNAMIC_DAILY_BUDGET")
        if "bid_strategy" not in values:
            values["bid_strategy"] = (
                "TARGET_ROAS"
                if values.get("roas_bid") is not None
                or values.get("deep_bid_type") == "VO_MIN_ROAS"
                else "HIGHEST_VALUE"
            )
        return values

    @model_validator(mode="after")
    def validate_budget_and_bid_contract(self) -> Self:
        if self.budget_strategy == "ADGROUP":
            if self.budget is None:
                raise ValueError("adgroup budget requires an ad group budget")
            if self.budget_mode != "BUDGET_MODE_DYNAMIC_DAILY_BUDGET":
                raise ValueError("adgroup budget requires dynamic daily budget mode")
        elif self.budget is not None or self.budget_mode is not None:
            raise ValueError("series budget must remain on the campaign")
        if self.bid_strategy == "HIGHEST_VALUE":
            if self.roas_bid is not None or self.deep_bid_type != "VO_HIGHEST_VALUE":
                raise ValueError("highest value must not include a ROAS bid")
        else:
            if self.roas_bid is None or self.deep_bid_type != "VO_MIN_ROAS":
                raise ValueError("target ROAS requires an exact ROAS bid")
        return self


class AdGroupCreate(AdGroupObservedFacts):
    operation_status: Literal["ENABLE"] = "ENABLE"

    @model_validator(mode="after")
    def require_strict_targeting(self) -> Self:
        if (
            any(
                value is not None
                for value in (self.languages, self.age_groups, self.gender)
            )
            and self.targeting_optimization_mode != "MANUAL"
        ):
            raise ValueError("自定义定向必须显式使用 MANUAL，避免平台忽略用户选择")
        return self


class CreativeAsset(FrozenModel):
    video_id: Id
    image_id: Id | None = None
    file_name: str | None = Field(default=None, min_length=1, max_length=100)


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
    def disable_adgroup(
        self, *, advertiser_id: str, adgroup_id: str
    ) -> McpBusinessResponse: ...
    def list_optimizer_rules(
        self, *, advertiser_id: str, page: int = 1
    ) -> McpBusinessResponse: ...
    def create(self, *, attempt_id: UUID, intent: CreateIntent) -> CreatedObject: ...
    def read_page(self, *, query: BuildReadQuery) -> BuildPage: ...
    def read_adgroup_status(
        self, *, advertiser_id: str, adgroup_id: str
    ) -> AdGroupStatus: ...
