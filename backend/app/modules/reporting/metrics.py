# backend/app/modules/reporting/metrics.py
"""平台报表字段 → 存储列。

基础报表 dN = 点击后 N+1 天累计，因此 D3/D7/D14/D30 取 d2/d6/d13/d29；
创意与广告使用同一套基础报表字段，事实粒度分别保存。
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.core.errors import DomainError
from app.modules.reporting.metric_catalog import basic_fields

_BASIC = basic_fields()
COLUMNS = tuple(_BASIC)

# 广告报表随行带回上级 ID、名称和类型，新建对象无需等待目录同步即可归组。
AD_DAY_METRICS = (
    *_BASIC.values(),
    "campaign_id",
    "adgroup_id",
    "campaign_name",
    "adgroup_name",
    "ad_name",
    "campaign_automation_type",
)
CAMPAIGN_METRICS = (*_BASIC.values(), "campaign_name")
ACCOUNT_METRICS = ("spend", "native_growth_total_ad_impression_value")


def parse_decimal(value: object) -> Decimal | None:
    if value is None or value == "-":
        return None
    if type(value) not in (str, int):
        raise DomainError("report_value_invalid", "报表指标必须是定点数字文本")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DomainError("report_value_invalid", "报表指标不是有效定点数") from exc
    if not amount.is_finite():
        raise DomainError("report_value_invalid", "报表指标不能是无穷值")
    return amount


def basic_metric_values(metrics: dict[str, object]) -> dict[str, Decimal | None]:
    return {
        column: parse_decimal(metrics.get(field)) for column, field in _BASIC.items()
    }


def parse_local_day(raw: object) -> date:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError as exc:
        raise DomainError("report_row_invalid", "报表日期维度无效") from exc


def parse_local_hour(raw: object, timezone: str) -> datetime:
    try:
        local = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
        return local.replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    except (ValueError, KeyError) as exc:
        raise DomainError("report_row_invalid", "报表小时维度无效") from exc
