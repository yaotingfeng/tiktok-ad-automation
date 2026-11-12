"""Validation and Excel/TSV contracts for daily drama plans."""

from datetime import date, time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class DailyPlanRowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    business_date: date
    scheduled_time: time
    title: str = Field(min_length=1, max_length=1000)
    provider_connection_id: UUID
    application_id: str = Field(min_length=1, max_length=255)
    promotion_url: str | None = Field(default=None, max_length=8192)
    external_drama_id: str | None = Field(default=None, max_length=255)
    order_materials: bool = False
    optimizer_id: UUID

    @model_validator(mode="after")
    def normalize_text(self) -> DailyPlanRowInput:
        self.title = self.title.strip()
        self.application_id = self.application_id.strip()
        if self.external_drama_id is not None:
            self.external_drama_id = self.external_drama_id.strip() or None
        if self.promotion_url is not None:
            self.promotion_url = self.promotion_url.strip() or None
            if self.promotion_url is not None:
                parsed = HttpUrl(self.promotion_url)
                if parsed.scheme != "https":
                    raise ValueError("promotion_url must use HTTPS")
        if not self.title:
            raise ValueError("title is required")
        if not self.application_id:
            raise ValueError("application_id is required")
        return self


class DailyPlanRequest(BaseModel):
    """A complete day replacement, guarded by the parent revision on PATCH."""

    model_config = ConfigDict(extra="forbid")

    business_date: date | None = None
    rows: list[DailyPlanRowInput] | None = Field(default=None, max_length=1000)
    tsv: str | None = Field(default=None, max_length=2_000_000)
    revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_input(self) -> DailyPlanRequest:
        if self.rows is None and self.tsv is None:
            raise ValueError("rows or tsv is required")
        if self.rows is not None and self.tsv is not None:
            raise ValueError("rows and tsv are mutually exclusive")
        return self


class DailyPlanRowPublic(DailyPlanRowInput):
    id: UUID
    plan_id: UUID
    position: int
    read_only: bool = False
    last_dispatch_error: str | None = None


class DailyPlanPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    optimizer_id: UUID
    business_date: date
    revision: int
    rows: list[DailyPlanRowPublic]


class DailyPlanPage(BaseModel):
    items: list[DailyPlanPublic]
    total: int


TSV_COLUMNS = (
    "business_date",
    "scheduled_time",
    "title",
    "provider_connection_id",
    "application_id",
    "promotion_url",
    "external_drama_id",
    "order_materials",
    "optimizer_id",
)


def _clipboard_rows(value: str) -> list[list[str]]:
    """Parse Excel TSV while retaining quoted tabs/newlines and leading zeros."""
    rows: list[list[str]] = []
    row: list[str] = []
    cell = ""
    quoted = False
    source = value.replace("\r\n", "\n").replace("\r", "\n")
    index = 0
    while index < len(source):
        char = source[index]
        if char == '"' and (quoted or not cell):
            if quoted and index + 1 < len(source) and source[index + 1] == '"':
                cell += '"'
                index += 2
                continue
            quoted = not quoted
        elif not quoted and char in "\t\n":
            row.append(cell)
            cell = ""
            if char == "\n":
                rows.append(row)
                row = []
        else:
            cell += char
        index += 1
    if quoted:
        raise ValueError("TSV contains an unterminated quoted cell")
    if cell or row or not source.endswith("\n"):
        row.append(cell)
        rows.append(row)
    return rows


def _bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "是"}:
        return True
    if normalized in {"0", "false", "no", "n", "否", ""}:
        return False
    raise ValueError("order_materials must be true/false")


def parse_plan_tsv(value: str) -> list[DailyPlanRowInput]:
    rows = _clipboard_rows(value)
    if not rows:
        return []
    # Accept a header row when it is copied from the plan grid; positional TSV
    # remains supported for direct Excel pastes.
    if [part.strip() for part in rows[0]] == list(TSV_COLUMNS):
        rows = rows[1:]
    if any(len(row) > len(TSV_COLUMNS) for row in rows):
        raise ValueError("TSV exceeds the nine daily plan columns")
    parsed: list[DailyPlanRowInput] = []
    for _index, raw in enumerate(rows):
        values = raw + [""] * (len(TSV_COLUMNS) - len(raw))
        if not any(value.strip() for value in values):
            continue
        payload: dict[str, Any] = dict(zip(TSV_COLUMNS, values, strict=True))
        payload["order_materials"] = _bool(payload["order_materials"])
        parsed.append(DailyPlanRowInput.model_validate(payload))
    validate_plan_rows(parsed)
    return parsed


def validate_plan_rows(rows: list[DailyPlanRowInput]) -> list[DailyPlanRowInput]:
    if len(rows) > 1000:
        raise ValueError("a daily plan can contain at most 1000 rows")
    seen: set[tuple[date, str]] = set()
    optimizer_ids = {row.optimizer_id for row in rows}
    if len(optimizer_ids) > 1:
        raise ValueError("all plan rows must use one optimizer")
    for row in rows:
        key = (row.business_date, row.title)
        if key in seen:
            raise ValueError("title must be unique within a business day")
        seen.add(key)
    return rows
