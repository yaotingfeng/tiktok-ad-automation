"""自动规则定义与接口模型。定义整体存 JSONB,每次运行保存版本快照。"""

from datetime import datetime, time
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.automation_rules.catalog import (
    RULE_METRICS,
    allowed_action_levels,
    min_frequency_minutes,
)

Level = Literal["campaign", "adgroup", "ad", "material"]
FREQUENCY_MINUTES = {"30M": 30, "1H": 60, "2H": 120, "4H": 240, "DAILY": 1440}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountScope(_Strict):
    mode: Literal["ALL", "BC", "ACCOUNTS"]
    bc_ids: tuple[str, ...] = ()
    advertiser_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.mode == "BC" and not self.bc_ids:
            raise ValueError("bc_ids required")
        if self.mode == "ACCOUNTS" and not self.advertiser_ids:
            raise ValueError("advertiser_ids required")
        return self


class ApplyTo(_Strict):
    level: Level
    status: Literal["ACTIVE", "PAUSED", "SELECTED"]
    # 指定对象:"账户ID:远端ID";素材为"账户ID:主素材ID"。
    object_ids: tuple[str, ...] = ()
    drama_names: tuple[str, ...] = ()
    name_contains: str | None = Field(default=None, max_length=200)
    name_excludes: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.status == "SELECTED" and not self.object_ids:
            raise ValueError("object_ids required")
        return self


class Condition(_Strict):
    metric: str
    range: (
        Literal[
            "TODAY",
            "YESTERDAY",
            "PAST_3_DAYS",
            "PAST_5_DAYS",
            "PAST_7_DAYS",
            "LIFETIME",
        ]
        | None
    ) = None
    operator: Literal[
        "GT",
        "LT",
        "BETWEEN",
        "EQ",
        "CONTAINS",
        "NOT_CONTAINS",
        "STARTS_WITH",
        "ENDS_WITH",
    ]
    values: tuple[str, ...] = Field(min_length=1, max_length=2)


class Adjustment(_Strict):
    mode: Literal["INCREASE", "DECREASE", "SET"]
    value_type: Literal["AMOUNT", "PERCENT"]
    value: Decimal = Field(gt=0)
    limit: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check(self) -> Self:
        # 提高必须设上限、降低必须设下限,防止预算或出价失控。
        if self.mode != "SET" and self.limit is None:
            raise ValueError("limit required")
        if self.mode == "SET" and (
            self.value_type != "AMOUNT" or self.limit is not None
        ):
            raise ValueError("SET takes an absolute value without limit")
        return self


class Frequency(_Strict):
    type: Literal["ONCE", "EVERY_24H", "EVERY_48H", "EVERY_WEEK", "CUSTOM"]
    window: int | None = Field(default=None, ge=1, le=10000)
    unit: Literal["MINUTE", "HOUR", "DAY"] | None = None
    count: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def check(self) -> Self:
        custom = (self.window, self.unit, self.count)
        if (self.type == "CUSTOM") != all(v is not None for v in custom):
            raise ValueError("custom frequency needs window, unit and count")
        return self


class Action(_Strict):
    type: Literal["PAUSE", "ENABLE", "BUDGET", "ROAS"]
    level: Level
    adjustment: Adjustment | None = None
    frequency: Frequency | None = None

    @model_validator(mode="after")
    def check(self) -> Self:
        adjusts = self.type in ("BUDGET", "ROAS")
        if adjusts != (self.adjustment is not None) or adjusts != (
            self.frequency is not None
        ):
            raise ValueError(
                "budget/roas need adjustment and frequency; pause/enable take none"
            )
        return self


class Period(_Strict):
    date_type: Literal["DAY", "WEEK", "MONTH"]
    days: tuple[int, ...] = ()
    start: time
    end: time

    @model_validator(mode="after")
    def check(self) -> Self:
        limit = {"DAY": 0, "WEEK": 7, "MONTH": 28}[self.date_type]
        if self.date_type != "DAY" and not self.days:
            raise ValueError("days required")
        if any(not 1 <= day <= limit for day in self.days) or self.start > self.end:
            raise ValueError("invalid period")
        return self


class Schedule(_Strict):
    frequency: Literal["30M", "1H", "2H", "4H", "DAILY"]
    daily_at: time | None = None
    window: Literal["CONTINUOUS", "CUSTOM", "ONCE"]
    periods: tuple[Period, ...] = Field(default=(), max_length=7)
    once_at: datetime | None = None

    @model_validator(mode="after")
    def check(self) -> Self:
        if (self.frequency == "DAILY") != (self.daily_at is not None):
            raise ValueError("daily_at only and always with DAILY")
        if (self.window == "CUSTOM") != bool(self.periods):
            raise ValueError("periods only and always with CUSTOM")
        if (self.window == "ONCE") != (self.once_at is not None):
            raise ValueError("once_at only and always with ONCE")
        if self.once_at is not None and self.once_at.tzinfo is None:
            raise ValueError("once_at must carry a timezone")
        return self


class NotificationSetting(_Strict):
    enabled: bool = True
    timing: Literal["EVERY_RUN"] = "EVERY_RUN"
    notify_without_hits: bool = False


def _check_condition(condition: Condition, *, level: str, aggregation: str) -> None:
    metric = RULE_METRICS.get(condition.metric)
    if metric is None:
        raise ValueError(f"unknown metric {condition.metric}")
    if level not in metric.levels or aggregation not in metric.aggregations:
        raise ValueError(f"{condition.metric} unavailable here")
    if condition.operator not in metric.operators:
        raise ValueError(f"{condition.operator} unavailable for {condition.metric}")
    if metric.ranged != (condition.range is not None):
        raise ValueError("range required for facts and forbidden for attributes")
    if (condition.operator == "BETWEEN") != (len(condition.values) == 2):
        raise ValueError("BETWEEN takes exactly two values")
    if metric.key != "name":
        try:
            numbers = [Decimal(v) for v in condition.values]
        except ArithmeticError as exc:
            raise ValueError("numeric values required") from exc
        if any(not n.is_finite() or n < 0 for n in numbers):
            raise ValueError("values must be finite and non-negative")
        if len(numbers) == 2 and numbers[0] >= numbers[1]:
            raise ValueError("BETWEEN lower bound must be below upper bound")
    elif not all(0 < len(v) <= 512 for v in condition.values):
        raise ValueError("name values must be 1-512 characters")


class RuleDefinition(_Strict):
    accounts: AccountScope
    apply_to: ApplyTo
    aggregation: Literal["EACH", "DRAMA", "ALL"]
    conditions: tuple[Condition, ...] = Field(min_length=1, max_length=5)
    action: Action
    mode: Literal["AUTO", "CONFIRM", "NOTIFY"]
    max_targets: int = Field(default=200, ge=1, le=5000)
    schedule: Schedule
    notification: NotificationSetting = NotificationSetting()

    @model_validator(mode="after")
    def check(self) -> Self:
        level = self.apply_to.level
        if level == "material" and self.aggregation != "EACH":
            raise ValueError("material rules judge each material")
        for condition in self.conditions:
            _check_condition(condition, level=level, aggregation=self.aggregation)
        if self.action.level not in allowed_action_levels(
            self.aggregation, level, self.action.type
        ):
            raise ValueError("action level outside the judged scope")
        minimum = min_frequency_minutes(level, self.action.level)
        if FREQUENCY_MINUTES[self.schedule.frequency] < minimum:
            raise ValueError(f"run at most every {minimum} minutes")
        return self


class AutomationSettingsPublic(BaseModel):
    schedule_timezone: str
    paused: bool
    reverse_cooldown_hours: int
    daily_increase_limit_pct: int
    feishu_configured: bool
    feishu_enabled: bool
    feishu_url_hint: str | None
    feishu_has_secret: bool


class AutomationSettingsUpdate(_Strict):
    schedule_timezone: str = Field(max_length=64)
    paused: bool
    feishu_enabled: bool
    reverse_cooldown_hours: int = Field(default=24, ge=1, le=168)
    daily_increase_limit_pct: int = Field(default=100, ge=1, le=1000)
    webhook_url: str | None = Field(default=None, max_length=300)
    secret: str | None = Field(default=None, max_length=200)
    clear_feishu: bool = False

    @field_validator("schedule_timezone")
    @classmethod
    def zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown timezone") from exc
        return value


class FeishuTestRequest(_Strict):
    webhook_url: str | None = Field(default=None, max_length=300)
    secret: str | None = Field(default=None, max_length=200)


class RuleCreate(_Strict):
    name: str = Field(min_length=1, max_length=100)
    definition: RuleDefinition
    enabled: bool = True


class RuleUpdate(RuleCreate):
    expected_version: int = Field(ge=1)


class RuleEnabled(_Strict):
    enabled: bool


class RunSummary(BaseModel):
    id: UUID
    rule_id: UUID
    slot_at: datetime
    status: str
    started_at: datetime
    finished_at: datetime | None
    checked_count: int
    hit_count: int
    skipped_count: int
    skipped_accounts: list[dict[str, str]]
    error_code: str | None
    notify_status: str
    notify_error: str | None
    mode: str
    execution_status: str
    execution_summary: dict[str, Any]


class RulePublic(BaseModel):
    id: UUID
    name: str
    enabled: bool
    version: int
    definition: RuleDefinition
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run: RunSummary | None = None


class RulePage(BaseModel):
    items: list[RulePublic]
    total: int


class HitPublic(BaseModel):
    subject_key: str
    subject_label: str
    drama_name: str | None
    advertiser_id: str | None
    bc_id: str | None
    currency: str | None
    metrics: dict[str, str]
    target_count: int
    targets: list[dict[str, Any]]
    # 执行结果；预演与仅通知运行为 None 或 NOTIFIED。
    outcome: str | None = None


class DryRunRequest(_Strict):
    definition: RuleDefinition


class DryRunPublic(BaseModel):
    checked_count: int
    hit_count: int
    skipped_count: int
    skipped_accounts: list[dict[str, str]]
    hits: list[HitPublic]


class CatalogMetric(BaseModel):
    key: str
    group: str
    name_zh: str
    unit: str
    kind: str
    levels: list[str]
    aggregations: list[str]
    operators: list[str]
    ranged: bool


class CatalogPublic(BaseModel):
    metrics: list[CatalogMetric]
    action_levels: dict[str, list[str]]


class RunListItem(RunSummary):
    rule_name: str


class RunPage(BaseModel):
    items: list[RunListItem]
    total: int


class RunDetail(BaseModel):
    run: RunListItem
    hits: list[HitPublic]
    # 该运行生成的待确认批次（如有），供前端跳转确认页。
    confirmation_id: UUID | None = None


class ConfirmationItem(BaseModel):
    id: UUID
    rule_id: UUID
    rule_name: str
    run_id: UUID
    status: str
    created_at: datetime
    hit_count: int
    target_count: int


class ConfirmationPage(BaseModel):
    items: list[ConfirmationItem]
    total: int


class ConfirmationHit(BaseModel):
    subject_key: str
    subject_label: str
    drama_name: str | None
    advertiser_id: str | None
    target_count: int
    metrics_then: dict[str, str]
    metrics_now: dict[str, str]
    # 用最新数据重新判断后是否仍满足条件；不满足的默认不勾选。
    still_matching: bool


class ConfirmationDetail(BaseModel):
    confirmation: ConfirmationItem
    action: Action
    skipped_accounts: list[dict[str, str]]
    items: list[ConfirmationHit]


class ConfirmRequest(_Strict):
    subject_keys: list[str] = Field(min_length=1, max_length=5000)


class TemplatePublic(BaseModel):
    key: str
    name: str
    description: str
    definition: RuleDefinition


class TemplatePage(BaseModel):
    items: list[TemplatePublic]
