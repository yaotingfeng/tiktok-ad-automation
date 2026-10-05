from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.modules.builds.targeting_schemas import AudienceTargeting
from app.modules.strategies.naming import DEFAULT_NAME_TEMPLATE

# 固定数量会直接展开为内存计划。100 既覆盖当前产品的正常批量策略，也给
# 单次预览保留明确的工程边界，避免恶意或误填整数导致无界 tuple 分配。
MAX_FIXED_GROUP_COUNT = 100
MAX_FIXED_ADS_PER_GROUP = 100


def exact_decimal(value: Any) -> Any:
    if isinstance(value, (float, bool)):
        raise ValueError("Use a decimal string, never a binary float")
    return value


Money = Annotated[
    Decimal,
    BeforeValidator(exact_decimal),
    Field(gt=0, max_digits=38, decimal_places=12, allow_inf_nan=False),
]


class StrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    budget: Money
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    budget_strategy: Literal["SERIES", "ADGROUP"] = "SERIES"
    bid_strategy: Literal["HIGHEST_VALUE", "TARGET_ROAS"] = "HIGHEST_VALUE"
    targeting: AudienceTargeting = Field(default_factory=AudienceTargeting)
    group_generation_mode: Literal["FIXED", "BY_MATERIAL"] = "FIXED"
    group_count: int | None = Field(
        default=1, gt=0, le=MAX_FIXED_GROUP_COUNT, strict=True
    )
    group_material_allocation: Literal["SHARED", "SEQUENTIAL_AVERAGE"] | None = (
        "SHARED"
    )
    max_materials_per_group: int | None = Field(default=None, gt=0, strict=True)
    ad_generation_mode: Literal["FIXED", "BY_MATERIAL"] = "BY_MATERIAL"
    ads_per_group: int | None = Field(
        default=None, gt=0, le=MAX_FIXED_ADS_PER_GROUP, strict=True
    )
    ad_material_allocation: Literal["SHARED", "SEQUENTIAL_AVERAGE"] | None = None
    max_materials_per_ad: int | None = Field(default=1, gt=0, strict=True)
    creative_count: int = Field(default=1, gt=0, strict=True)
    target_roas: Money | None = None
    copy_pool_version: UUID
    cta_option_ids: tuple[str, ...] = ()
    campaign_name_template: str = Field(default=DEFAULT_NAME_TEMPLATE, max_length=1000)

    @model_validator(mode="after")
    def validate_structure(self) -> StrategyConfig:
        """确保每一层只携带与生成模式对应的数量和素材分配。"""
        if self.group_generation_mode == "FIXED":
            if self.group_count is None:
                raise ValueError("group_count is required for FIXED groups")
            if self.max_materials_per_group is not None:
                raise ValueError(
                    "max_materials_per_group is only valid for BY_MATERIAL groups"
                )
        else:
            if self.max_materials_per_group is None:
                raise ValueError(
                    "max_materials_per_group is required for BY_MATERIAL groups"
                )
            if self.group_count is not None:
                raise ValueError(
                    "group_count is only valid for FIXED groups"
                )
            if self.group_material_allocation is not None:
                raise ValueError(
                    "group_material_allocation is only valid for FIXED groups"
                )

        if self.ad_generation_mode == "FIXED":
            if self.ads_per_group is None:
                raise ValueError("ads_per_group is required for FIXED ads")
            if self.max_materials_per_ad is not None:
                raise ValueError(
                    "max_materials_per_ad is only valid for BY_MATERIAL ads"
                )
        else:
            if self.max_materials_per_ad is None:
                raise ValueError(
                    "max_materials_per_ad is required for BY_MATERIAL ads"
                )
            if self.ads_per_group is not None:
                raise ValueError("ads_per_group is only valid for BY_MATERIAL ads")
            if self.ad_material_allocation is not None:
                raise ValueError(
                    "ad_material_allocation is only valid for BY_MATERIAL ads"
                )

        if self.bid_strategy == "TARGET_ROAS" and self.target_roas is None:
            raise ValueError("target_roas is required for TARGET_ROAS")
        if self.bid_strategy == "HIGHEST_VALUE" and self.target_roas is not None:
            raise ValueError("target_roas is only valid for TARGET_ROAS")
        return self


class ValidationIssue(BaseModel):
    field: str
    code: str


class StrategyPublic(BaseModel):
    id: UUID
    name: str
    active: bool
    latest_version: int
    version_id: UUID
    config: StrategyConfig
    created_by: UUID
    created_at: datetime


class VersionPublic(BaseModel):
    id: UUID
    strategy_id: UUID
    number: int
    config: StrategyConfig
    created_by: UUID
    created_at: datetime
    request_id: UUID


class CreateStrategyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    config: StrategyConfig
    request_id: UUID


class AppendVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: StrategyConfig
    expected_version: int = Field(ge=1, strict=True)
    request_id: UUID


class StrategyStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool | None = Field(default=None, strict=True)
    name: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def require_change(self) -> StrategyStateRequest:
        if self.active is None and self.name is None:
            raise ValueError("At least one strategy field is required")
        return self


class CopyPublic(BaseModel):
    id: UUID
    text: str
    position: int


class CopyPoolPublic(BaseModel):
    id: UUID
    name: str
    entries: list[CopyPublic]


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationIssue]
    scene_check_pending: bool = True
