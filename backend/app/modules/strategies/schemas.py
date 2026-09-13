from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.modules.strategies.naming import DEFAULT_NAME_TEMPLATE


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
    target_roas: Money
    group_size: int = Field(gt=0, strict=True)
    creative_count: int = Field(gt=0, strict=True)
    copy_pool_version: UUID
    cta_option_ids: tuple[str, ...] = ()
    campaign_name_template: str = Field(default=DEFAULT_NAME_TEMPLATE, max_length=1000)


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
    config: StrategyConfig
    expected_version: int = Field(ge=1, strict=True)
    request_id: UUID


class StrategyStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool = Field(strict=True)


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
