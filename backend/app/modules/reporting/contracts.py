"""经文档核验的原始指标合同；通道合同通过不代表账户已经授权或真实联调成功。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.integrations.tiktok.contracts.reporting import ReportQuery


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    unit: str
    additivity: str
    supported_contracts: tuple[str, ...]


@dataclass(frozen=True)
class ReportContract:
    key: str
    metric_family: str
    dimensions: tuple[str, ...]
    metrics: tuple[str, ...]
    granularities: tuple[str, ...]
    empty_result_policy: str
    channels: tuple[str, ...] = ("OFFICIAL_API", "OFFICIAL_MCP")
    ad_types: tuple[str, ...] = ("REGULAR", "LEGACY_SMART_PLUS", "SMART_PLUS")
    subject_kind: str = ""
    # 相同金额指标在素材报告仍可能重叠；下游不能跨素材类型或层级求和。
    additivity: str = "SAME_GRAIN_ONLY"
    async_supported: bool = False


CORE_METRICS = (
    "spend",
    "native_growth_ad_revenue_value_d0",
    "native_growth_total_ad_impression_value",
    "native_growth_total_ad_impression_event_count",
    "impressions",
    "clicks",
)
DEFAULT_ATTRIBUTION = "default"
# D0 是点击后 24 小时收入；与总收入分别展示，不能彼此相加。
# 未传 minis_id 的基础维度合同为 32 天窗口。minis_id 改变归因，当前不开放。


def _basic(
    key: str, identity: str, kind: str, ad_types: tuple[str, ...]
) -> ReportContract:
    return ReportContract(
        key,
        "delivery",
        (identity, "stat_time_day", "stat_time_hour"),
        CORE_METRICS,
        ("RANGE", "DAY", "HOUR"),
        "REPLACE_PARTITION",
        ad_types=ad_types,
        subject_kind=kind,
    )


_BASIC = (
    _basic(
        "basic_account",
        "advertiser_id",
        "account",
        ("REGULAR", "LEGACY_SMART_PLUS", "SMART_PLUS"),
    ),
    _basic(
        "basic_campaign",
        "campaign_id",
        "campaign",
        ("REGULAR", "LEGACY_SMART_PLUS", "SMART_PLUS"),
    ),
    _basic(
        "basic_adgroup",
        "adgroup_id",
        "adgroup",
        ("REGULAR", "LEGACY_SMART_PLUS", "SMART_PLUS"),
    ),
    _basic("basic_ad", "ad_id", "ad", ("REGULAR", "LEGACY_SMART_PLUS")),
    _basic("basic_smart_plus_ad", "ad_id_v2", "ad", ("SMART_PLUS",)),
    _basic("basic_smart_plus_creative", "ad_id", "creative", ("SMART_PLUS",)),
)
_MATERIAL = (
    ReportContract(
        "material_overview",
        "material",
        (
            "advertiser_id",
            "campaign_id",
            "adgroup_id",
            "smart_plus_ad_id",
            "main_material_id",
        ),
        ("spend", "impressions", "clicks"),
        ("RANGE",),
        "REPLACE_PARTITION",
        ad_types=("SMART_PLUS",),
        subject_kind="material",
        additivity="NON_ADDITIVE_MATERIAL",
    ),
    ReportContract(
        "material_breakdown",
        "material",
        ("main_material_id", "stat_time_day", "stat_time_hour"),
        ("spend", "impressions", "clicks"),
        ("RANGE", "DAY", "HOUR"),
        "REPLACE_PARTITION",
        ad_types=("SMART_PLUS",),
        subject_kind="material",
        additivity="NON_ADDITIVE_MATERIAL",
    ),
)
REPORT_CONTRACTS: Mapping[str, ReportContract] = MappingProxyType(
    {item.key: item for item in (*_BASIC, *_MATERIAL)}
)
METRIC_DEFINITIONS: Mapping[str, MetricDefinition] = MappingProxyType(
    {
        name: MetricDefinition(
            name,
            "CURRENCY" if name in CORE_METRICS[:3] else "COUNT",
            "SUM",
            tuple(
                item.key for item in REPORT_CONTRACTS.values() if name in item.metrics
            ),
        )
        for name in CORE_METRICS
    }
    | {
        # 优化结果/转化依赖目标和事件定义；当前 Query 没有这种身份维度，因此不开放写入。
        name: MetricDefinition(name, "COUNT", "GOAL_AND_EVENT_DEPENDENT", ())
        for name in ("result", "conversion")
    }
)
METRICS = METRIC_DEFINITIONS
CONTRACTS = REPORT_CONTRACTS


def get_metric_definition(name: str) -> MetricDefinition:
    try:
        return METRIC_DEFINITIONS[name]
    except KeyError as exc:
        raise ValueError("unsupported report metric") from exc


def get_report_contract(key: str) -> ReportContract:
    if key not in REPORT_CONTRACTS:
        raise ValueError("unsupported report contract")
    return REPORT_CONTRACTS[key]


def supports_metric(*, report_contract: str, metric_name: str) -> bool:
    metric = METRIC_DEFINITIONS.get(metric_name)
    return metric is not None and report_contract in metric.supported_contracts


def validate_query(
    query: ReportQuery, *, channel: str, ad_type: str | None = None
) -> ReportContract:
    """只开放固定 ID/时间组合；不把同端点的任意维度或异步能力自动放行。"""
    contract = get_report_contract(query.report_contract)
    # ReportQuery 是稳定的公共 DTO；广告类型作为持久化 run 查询的额外路由字段
    # 校验，而不是塞入 DTO。受限合同必须显式声明类型，避免 basic_ad/material
    # 在缺省值下绕过合同矩阵。
    restricted_ad_type_contracts = {
        "basic_ad",
        "basic_smart_plus_ad",
        "basic_smart_plus_creative",
        "material_overview",
        "material_breakdown",
    }
    if channel not in contract.channels or (
        query.report_contract in restricted_ad_type_contracts
        and (not isinstance(ad_type, str) or not ad_type)
    ) or (ad_type is not None and ad_type not in contract.ad_types):
        raise ValueError("unsupported channel or advertising type")
    if (
        query.metric_family != contract.metric_family
        or query.granularity not in contract.granularities
    ):
        raise ValueError("unsupported metric family or granularity")
    if query.attribution != DEFAULT_ATTRIBUTION:
        raise ValueError("unsupported attribution")
    if not set(query.metrics) <= set(contract.metrics):
        raise ValueError("unsupported metrics")
    try:
        ZoneInfo(query.timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("invalid report timezone") from exc
    time_dimension = {
        "DAY": ("stat_time_day",),
        "HOUR": ("stat_time_hour",),
        "RANGE": (),
    }[query.granularity]
    if query.report_contract == "material_overview":
        valid = (
            len(query.dimensions) == 3
            and query.dimensions[0] in contract.dimensions[:-2]
            and query.dimensions[1:] == ("main_material_id", "main_material_type")
        )
    elif query.report_contract == "material_breakdown":
        time_dimension = {
            "DAY": ("stat_time_day",),
            "HOUR": ("stat_time_hour",),
            "RANGE": (),
        }[query.granularity]
        valid = query.dimensions == ("main_material_id", "main_material_type", *time_dimension)
    else:
        valid = query.dimensions == (contract.dimensions[0], *time_dimension)
    if not valid:
        raise ValueError("unsupported dimension combination")
    if query.report_contract == "material_breakdown" and query.filter_ids:
        # 官方 breakdown 单广告/时间筛选的说明与矩阵不一致，不能替换成账户总额。
        raise ValueError("material breakdown filtered scope is unverified")
    days = (query.end_date - query.start_date).days + 1
    limit = (
        (7 if query.report_contract == "material_breakdown" else 1)
        if query.granularity == "HOUR"
        else (
            30
            if query.granularity == "DAY" and contract.subject_kind != "material"
            else 365
        )
    )
    if days > limit:
        raise ValueError("report range requires sharding")
    return contract


def query_payload(query: ReportQuery, *, ad_type: str | None = None) -> dict:
    payload = {
        "advertiser_id": query.advertiser_id,
        "report_contract": query.report_contract,
        "metric_family": query.metric_family,
        "dimensions": list(query.dimensions),
        "metrics": sorted(query.metrics),
        "start_date": query.start_date.isoformat(),
        "end_date": query.end_date.isoformat(),
        "granularity": query.granularity,
        "currency": query.currency,
        "timezone": query.timezone,
        "attribution": query.attribution,
        "filter_ids": sorted(query.filter_ids),
        "page": query.page,
    }
    if ad_type is not None:
        if type(ad_type) is not str or not ad_type.strip():
            raise ValueError("invalid persisted advertising type")
        payload["ad_type"] = ad_type.strip().upper()
    return payload


def decode_query(value: dict) -> ReportQuery:
    if type(value) is not dict:
        raise ValueError("invalid persisted report query")
    # ad_type belongs to the persisted run admission boundary, not ReportQuery's
    # stable DTO. Keep accepting it here so publication can validate the matrix.
    persisted = dict(value)
    persisted.pop("ad_type", None)
    expected = set(ReportQuery.__dataclass_fields__)
    if not set(persisted) <= expected or set(persisted) - expected - {"page"}:
        raise ValueError("invalid persisted report query")
    data = persisted
    data.setdefault("page", 1)
    for key in ("dimensions", "metrics", "filter_ids"):
        if type(data[key]) is not list:
            raise ValueError("invalid persisted report list")
        data[key] = tuple(data[key])
    for key in ("start_date", "end_date"):
        data[key] = date.fromisoformat(data[key])
    return ReportQuery(**data)


def report_partition_key(query: ReportQuery, *, ad_type: str | None = None) -> str:
    payload = query_payload(query, ad_type=ad_type)
    payload.pop("page")
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def report_buckets(query: ReportQuery) -> tuple[tuple[datetime, datetime], ...]:
    """查询日期包含末日；本地日边界转 UTC 后再分小时，保留夏令时 23/25 小时。"""
    zone = ZoneInfo(query.timezone)
    start = datetime.combine(query.start_date, time.min, zone).astimezone(UTC)
    end = datetime.combine(
        query.end_date + timedelta(days=1), time.min, zone
    ).astimezone(UTC)
    if query.granularity == "RANGE":
        return ((start, end),)
    if query.granularity == "HOUR":
        return tuple(
            (start + timedelta(hours=i), min(start + timedelta(hours=i + 1), end))
            for i in range(int((end - start).total_seconds() / 3600))
        )
    return tuple(
        (
            datetime.combine(
                query.start_date + timedelta(days=i), time.min, zone
            ).astimezone(UTC),
            datetime.combine(
                query.start_date + timedelta(days=i + 1), time.min, zone
            ).astimezone(UTC),
        )
        for i in range((query.end_date - query.start_date).days + 1)
    )
