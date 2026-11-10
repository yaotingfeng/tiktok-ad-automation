"""复制配置只接受明确覆盖；None 表示沿用每条来源实际值。"""

from decimal import Decimal
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.integrations.tiktok.contracts.builds import _money
from app.modules.builds.schemas import DeliveryConfig
from app.modules.builds.targeting_schemas import AudienceTargeting

ExactPositive = Annotated[
    Decimal,
    BeforeValidator(_money),
    Field(gt=0, max_digits=38, decimal_places=12, allow_inf_nan=False),
]


class CopyConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_advertiser_ids: list[str] = Field(min_length=1, max_length=100)
    copies: int = Field(default=1, ge=1, le=20, strict=True)
    budget: ExactPositive | None = None
    target_roas: ExactPositive | None = None
    targeting: AudienceTargeting | None = None
    delivery_config: DeliveryConfig = Field(default_factory=DeliveryConfig)
    material_ids: list[UUID] = Field(min_length=1, max_length=10000)

    @field_validator("target_advertiser_ids")
    @classmethod
    def account_ids(cls, value: list[str]) -> list[str]:
        if any(not v.strip() or v != v.strip() or len(v) > 128 for v in value):
            raise ValueError("账户标识无效")
        return list(dict.fromkeys(value))

    @field_validator("material_ids")
    @classmethod
    def materials(cls, value: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(value))


class CopyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bc_id: str = Field(min_length=1, max_length=128)
    selection_id: UUID
    request_id: UUID


class CopyRowPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_id: UUID
    configuration: CopyConfiguration


class CopyRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: int = Field(ge=1, strict=True)


class CopyPatchRequest(CopyRevisionRequest):
    rows: list[CopyRowPatch] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_rows(self) -> Self:
        if len({r.row_id for r in self.rows}) != len(self.rows):
            raise ValueError("同一来源不能重复设置")
        return self


class CopySubmitRequest(CopyRevisionRequest):
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CopyRowPublic(BaseModel):
    row_id: UUID
    source: dict[str, Any]
    # 被阻断来源可能没有可复制素材，保留输入证据，编辑时才要求完整配置。
    configuration: dict[str, Any]


class CopyChildPublic(BaseModel):
    row_id: UUID
    advertiser_id: str
    ordinal: int
    preview_id: UUID | None
    submission_id: UUID | None
    status: str
    error_code: str | None
    group_settings: list[dict[str, Any]] = Field(default_factory=list)


class CopyDraftPublic(BaseModel):
    draft_id: UUID
    bc_id: str
    revision: int
    status: Literal["DRAFT", "PREPARING", "READY", "SUBMITTED", "FAILED"]
    rows: list[CopyRowPublic]
    preview_digest: str | None
    children: list[CopyChildPublic]
    submission_ids: list[UUID]
    error_code: str | None
