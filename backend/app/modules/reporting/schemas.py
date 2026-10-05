"""报表查询边界 DTO。

这些 DTO 只描述已发布的本地报表合同。预算模式、目标 ROAS 和创建时间来自
目录配置，D0 ROAS 来自已发布事实聚合；它们由查询层显式应用，不能伪装成
独立的经济事实列。
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
CANONICAL_METRIC_KEYS = frozenset(
    {
        "spend",
        "native_growth_ad_revenue_value_d0",
        "native_growth_total_ad_impression_value",
        "native_growth_total_ad_impression_event_count",
        "impressions",
        "clicks",
        # Derived only after canonical A buckets are aggregated.
        "d0_roas",
        "ad_revenue_roas",
        "cost_per_ad_impression_event",
        "ctr",
    }
)


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
    # Directory configuration, applied by the aggregation layer.
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

    @model_validator(mode="after")
    def validate_published_metrics(self) -> MetricVector:
        if self.optimization_goal is not None:
            raise ValueError("optimization_goal is not a published report coordinate")
        unknown = (set(self.values) | set(self.availability)) - CANONICAL_METRIC_KEYS
        if unknown:
            raise ValueError(f"unsupported report metric keys: {sorted(unknown)}")
        if set(self.values) != set(self.availability):
            raise ValueError("metric values and availability must have the same keys")
        return self


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
    coverage: dict[str, object] = Field(default_factory=dict)


class TrendPoint(BaseModel):
    """趋势分桶及其可比较的差值；缺失值保持 ``None``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket_start: datetime
    bucket_end: datetime
    values: dict[str, Decimal | None] = Field(default_factory=dict)
    availability: dict[str, Availability] = Field(default_factory=dict)
    delta: dict[str, Decimal] | None = None
    delta_reason: Literal["SCOPE_CHANGED", "DATE_CHANGED"] | None = None


class TrendPublic(BaseModel):
    """B2 对外趋势 DTO；coverage 显式区分完整、空结果和不完整。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    points: tuple[TrendPoint, ...] = ()
    coverage: dict[str, object] = Field(default_factory=dict)
    actual_interval_minutes: int | None = None
    delta_reason: Literal["SCOPE_CHANGED", "DATE_CHANGED"] | None = None


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


class SavedViewPublic(BaseModel):
    """当前操作者在一个租户/BC 下保存的私有筛选视图。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    name: str
    filters: ReportingFilter
    columns: tuple[str, ...] = ()
    created_at: datetime


class SavedViewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    filters: ReportingFilter
    columns: tuple[str, ...] = ()


class SavedViewPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = Field(default=None, min_length=1, max_length=128)
    filters: ReportingFilter | None = None
    columns: tuple[str, ...] | None = None


class ExportPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    status: Literal["QUEUED", "RUNNING", "COMPLETE", "FAILED", "EXPIRED"]
    coverage: dict[str, object] = Field(default_factory=dict)
    expires_at: datetime


class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=128)


class SyncRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    advertiser_ids: tuple[str, ...] = Field(default=())
    scope: Literal["directory", "active", "report", "history", "targeted"] = "report"
    start_date: date | None = None
    end_date: date | None = None
    refs: tuple[EntityRef, ...] = ()

    @model_validator(mode="after")
    def validate_dates(self) -> SyncRunRequest:
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date and end_date must be supplied together")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        if len(set(self.advertiser_ids)) != len(self.advertiser_ids):
            raise ValueError("advertiser_ids must be unique")
        if not self.advertiser_ids and self.scope not in {"active", "report", "history"}:
            raise ValueError("advertiser_ids are required for this sync scope")
        return self


class SyncRunPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    request_id: UUID
    status: str
    coverage: str
    error_code: str | None = None
    observed_at: datetime | None = None
    completed_at: datetime | None = None
