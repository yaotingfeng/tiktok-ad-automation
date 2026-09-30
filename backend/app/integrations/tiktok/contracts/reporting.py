"""双通道报表合同；金额精度、不可用值与完整性由适配器显式提供。"""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from .accounts import require_id
from .ads import (
    require_aware,
    require_page_state,
    require_positive_int,
    require_strings,
    require_text,
)
from .common import CallEvidence


@dataclass(frozen=True)
class ReportQuery:
    advertiser_id: str
    report_contract: str
    metric_family: str
    dimensions: tuple[str, ...]
    metrics: tuple[str, ...]
    start_date: date
    end_date: date
    granularity: str
    currency: str
    timezone: str
    attribution: str
    filter_ids: tuple[str, ...]
    page: int

    def __post_init__(self) -> None:
        require_id(self.advertiser_id)
        for value in (
            self.report_contract,
            self.metric_family,
            self.granularity,
            self.currency,
            self.timezone,
            self.attribution,
        ):
            require_text(value)
        require_strings(self.dimensions)
        require_strings(self.metrics, nonempty=True)
        require_strings(self.filter_ids)
        # 日期窗口不是带时刻的时间戳；拒绝 datetime 隐式充当 date。
        if (
            type(self.start_date) is not date
            or type(self.end_date) is not date
            or self.start_date > self.end_date
        ):
            raise ValueError("invalid report date range")
        require_positive_int(self.page)


@dataclass(frozen=True)
class ReportRow:
    subject_key: tuple[str, ...]
    bucket_start: datetime
    bucket_end: datetime
    values: dict[str, Decimal | None]
    availability: dict[str, str]
    # 仅允许适配器解析出的描述/关联字段；事实层会再次按白名单过滤。
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # subject_key 是有序复合键，允许不同位置拥有相同值。
        if type(self.subject_key) is not tuple or not self.subject_key:
            raise ValueError("invalid report subject")
        for value in self.subject_key:
            require_id(value)
        require_aware(self.bucket_start)
        require_aware(self.bucket_end)
        # 夏令时回拨可能使墙上时钟倒退；桶范围按真实时刻而非当地钟面排序。
        if self.bucket_start.astimezone(UTC) >= self.bucket_end.astimezone(UTC):
            raise ValueError("invalid report bucket range")
        if type(self.values) is not dict or type(self.availability) is not dict:
            raise ValueError("invalid report metric maps")
        if type(self.attributes) is not dict:
            raise ValueError("invalid report attributes")
        for name, amount in self.values.items():
            require_text(name)
            if amount is not None and (
                not isinstance(amount, Decimal) or not amount.is_finite()
            ):
                raise ValueError("report values require finite Decimal or None")
        for name, status in self.availability.items():
            require_text(name)
            require_text(status)
        # 缺失指标保留 None，不能转为零或借浮点数推断金额。
        object.__setattr__(self, "values", deepcopy(self.values))
        object.__setattr__(self, "availability", deepcopy(self.availability))
        object.__setattr__(self, "attributes", deepcopy(self.attributes))


@dataclass(frozen=True)
class ReportPage:
    rows: tuple[ReportRow, ...]
    next_page: int | None
    complete: bool
    evidence: CallEvidence
    page: int = 1

    def __post_init__(self) -> None:
        if type(self.rows) is not tuple or any(
            not isinstance(row, ReportRow) for row in self.rows
        ):
            raise ValueError("invalid report rows")
        require_page_state(self.page, self.next_page, self.complete, self.evidence)


@dataclass(frozen=True)
class ReportTask:
    task_id: str
    advertiser_id: str
    status: Literal["PENDING", "RUNNING", "READY", "FAILED"]
    query: ReportQuery

    def __post_init__(self) -> None:
        require_id(self.task_id)
        require_id(self.advertiser_id)
        if self.status not in ("PENDING", "RUNNING", "READY", "FAILED"):
            raise ValueError("invalid report task status")
        if (
            not isinstance(self.query, ReportQuery)
            or self.query.advertiser_id != self.advertiser_id
        ):
            raise ValueError("task must share query advertiser")


class ReportOperations(Protocol):
    def read_page(self, query: ReportQuery) -> ReportPage: ...
    def create_task(self, query: ReportQuery) -> ReportTask: ...
    def check_task(self, task: ReportTask) -> ReportTask: ...

    def download_task(self, task: ReportTask) -> ReportPage:
        """仅允许 READY 任务下载；整个文件成功解析才 complete=True。

        文件结果固定 page=1、next_page=None，不得切成虚构的平台分页。
        未就绪、下载失败或解析失败不能返回完整成功结果。
        """
        ...
