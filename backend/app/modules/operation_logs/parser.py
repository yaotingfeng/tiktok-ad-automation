"""官方操作日志CSV：保持行的多重性，未知格式不发布完整覆盖。"""

import csv
import hashlib
import io
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.errors import DomainError

PARSER_VERSION = 1
MAX_EXPORT_BYTES = 16 * 1024 * 1024
HEADER = (
    "Time",
    "log_object_type",
    "Object ID",
    "Object",
    "Operator",
    "Source",
    "App ID",
    "Activity details",
)
SUMMARIES = {"Budget changes", "Bid changes", "Status changes"}


@dataclass(frozen=True)
class OperationEvent:
    identity: str
    occurred_at: datetime
    object_type: str
    object_id: str
    details: tuple[dict[str, Any], ...]
    source: str
    attribution: str
    raw: dict[str, str]


def invalid() -> DomainError:
    return DomainError("operation_log_format_invalid", "操作日志格式未知或不完整")


def utc_window(start: date, end: date, timezone: str) -> tuple[datetime, datetime]:
    """半开区间；按实际UTC跨度限制，秋季30当地日可能超过30×24小时。"""
    zone = ZoneInfo(timezone)
    lower = datetime.combine(start, time.min, zone).astimezone(UTC)
    upper = datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC)
    if not timedelta(0) < upper - lower <= timedelta(days=30):
        raise DomainError("operation_log_range_invalid", "日志查询实际跨度须在30天以内")
    return lower, upper


def csv_content(content: str) -> str:
    if len(content.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise DomainError(
            "operation_log_export_too_large", "日志超过单次归档上限，须缩小窗口"
        )
    if content.lstrip().startswith("{"):
        try:
            wrapped = json.loads(content)
            content = wrapped["file_data"]
        except ValueError, KeyError, TypeError:
            raise invalid() from None
        if not isinstance(content, str):
            raise invalid()
        # 已留存官方导出回执的file_data是带b引号的CSV文本；绝不eval内容。
        if content.startswith("b'") and content.endswith("'"):
            content = content[2:-1]
    return content.lstrip("\ufeff")


def parse_export(content: str) -> tuple[OperationEvent, ...]:
    text = csv_content(content)
    counts: Counter[str] = Counter()
    summaries: dict[str, int] = {}
    events = []
    header_seen = False
    category_counts: Counter[str] = Counter()
    try:
        for row in csv.reader(io.StringIO(text), strict=True):
            if not row:
                continue
            if not header_seen:
                if tuple(row) == HEADER:
                    header_seen = True
                elif (
                    len(row) == 2
                    and row[0] in SUMMARIES
                    and row[1].isdigit()
                    and row[0] not in summaries
                ):
                    summaries[row[0]] = int(row[1])
                else:
                    raise invalid()
                continue
            if (
                len(row) != len(HEADER)
                or not 0 < len(row[2]) <= 128
                or not 0 < len(row[1]) <= 32
            ):
                raise invalid()
            raw = dict(zip(HEADER, row, strict=True))
            stamp = datetime.strptime(row[0], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            details = json.loads(row[-1])
            if (
                not isinstance(details, list)
                or not details
                or any(
                    not isinstance(d, dict)
                    or not isinstance(d.get("action"), str)
                    or not isinstance(d.get("name"), str)
                    for d in details
                )
            ):
                raise invalid()
            categories = set()
            for detail in details:
                name = detail["name"].strip().lower()
                if "budget" in name:
                    categories.add("Budget changes")
                if name in {"minimum roas", "target roas", "roas bid", "roas_bid"}:
                    categories.add("Bid changes")
                if detail["action"].strip().lower() == "change" and name in {
                    "campaign",
                    "ad group",
                    "ad",
                    "creative",
                    "status",
                    "operation status",
                    "review status",
                }:
                    categories.add("Status changes")
            category_counts.update(categories)
            # 摘要不含查询窗口；相同内容序号保留真实重复行，跨重读仍稳定。
            canonical = json.dumps(
                {**raw, "Activity details": details}, sort_keys=True, ensure_ascii=False
            )
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            counts[digest] += 1
            identity = f"{digest}:{counts[digest]}"
            events.append(
                OperationEvent(
                    identity,
                    stamp,
                    row[1].upper().replace(" ", ""),
                    row[2],
                    tuple(details),
                    row[5],
                    "UNKNOWN",
                    raw,
                )
            )
    except csv.Error, ValueError, TypeError, OverflowError:
        raise invalid() from None
    if not header_seen or (
        not events and (set(summaries) != SUMMARIES or any(summaries.values()))
    ):
        raise invalid()
    if set(summaries) != SUMMARIES:
        raise invalid()
    # 汇总计数是对象变更行数；不足说明下载被截断，不能发布“无操作”。
    if any(category_counts[k] != n for k, n in summaries.items()):
        raise invalid()
    return tuple(events)
