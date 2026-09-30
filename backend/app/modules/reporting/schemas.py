"""报表查询边界 DTO。

这些 DTO 只描述已发布的本地报表合同。平台尚未提供的预算、目标 ROAS
和创建时间字段保留为显式输入，以便 API 返回可解释的“不支持”，而不是
把不存在的事实列伪装成可过滤数据。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef

Dimension = Literal["account", "campaign", "adgroup", "ad", "material", "drama"]
Availability = Literal[
    "AVAILABLE", "MISSING", "UNAVAILABLE", "UNSUPPORTED", "FAILED"
]


def _finite(value: Decimal | None) -> Decimal | None:
    if value is not None and not value.is_finite():
        raise ValueError("numeric thresholds must be finite")
    return value


class ReportingFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: Dimension
    start_date: date
    end_date: date
    advertiser_ids: tuple[str, ...] = ()
    ids: tuple[str, ...] = ()
    query: str | None = None
    ad_types: tuple[str, ...] = ()
    operation_statuses: tuple[str, ...] = ()
    review_statuses: tuple[str, ...] = ()
    # A 暂无预算模式事实；非空值由 compile_filter 明确拒绝。
    budget_modes: tuple[str, ...] = ()
    created_from: date | None = None
    created_to: date | None = None
    naming_status: str | None = None
    min_spend: Decimal | None = None
    max_spend: Decimal | None = None
    min_d0_roas: Decimal | None = None
    max_d0_roas: Decimal | None = None
    min_target_roas: Decimal | None = None
    max_target_roas: Decimal | None = None
    sort_by: str = "row_key"
    sort_direction: Literal["asc", "desc"] = "asc"

    @field_validator(
        "min_spend",
        "max_spend",
        "min_d0_roas",
        "max_d0_roas",
        "min_target_roas",
        "max_target_roas",
        mode="before",
    )
    @classmethod
    def finite_threshold(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        try:
            return _finite(Decimal(str(value)))
        except Exception as exc:
            raise ValueError("numeric thresholds must be finite decimals") from exc

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @model_validator(mode="after")
    def validate_ranges(self) -> ReportingFilter:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from must be on or before created_to")
        for lower, upper in (
            (self.min_spend, self.max_spend),
            (self.min_d0_roas, self.max_d0_roas),
            (self.min_target_roas, self.max_target_roas),
        ):
            if lower is not None and upper is not None and lower > upper:
                raise ValueError("minimum threshold must not exceed maximum threshold")
        return self


class MetricVector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str
    timezone: str
    attribution: str
    # A 当前没有 optimization_goal 坐标；None 是唯一可发布值。
    optimization_goal: str | None = None
    values: dict[str, Decimal | None] = Field(default_factory=dict)
    availability: dict[str, Availability] = Field(default_factory=dict)


class ReportRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_key: str
    display: dict[str, str | None] = Field(default_factory=dict)
    refs: tuple[EntityRef, ...] = ()
    material_uses: tuple[MaterialUseRef, ...] = ()
    metric_buckets: tuple[MetricVector, ...] = ()
    capabilities: dict[str, bool] = Field(default_factory=dict)
    directory_versions: dict[str, int] = Field(default_factory=dict)
    membership_digest: str = ""


class QuerySnapshotPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    expires_at: datetime
    filters: ReportingFilter
    publication_versions: dict[str, int] = Field(default_factory=dict)


class AdsQueryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: QuerySnapshotPublic
    items: tuple[ReportRow, ...] = ()
    total: int = 0
    summary: dict[str, object] = Field(default_factory=dict)
    coverage: dict[str, object] = Field(default_factory=dict)
    next_cursor: str | None = None


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    mode: Literal["EXPLICIT", "ALL_MATCHING"]
    row_keys: tuple[str, ...] = ()
    excluded_row_keys: tuple[str, ...] = ()


class FrozenSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selection_id: UUID
    snapshot_id: UUID
    refs: tuple[EntityRef, ...] = ()
    material_uses: tuple[MaterialUseRef, ...] = ()
    membership_digest: str
    expires_at: datetime
