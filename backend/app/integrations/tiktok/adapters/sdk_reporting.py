"""官方 SDK 报表适配器。

报表响应的 envelope、分页和下载文件边界在这里一次性校验。SDK 的
``async_req`` 只是本地线程包装，不能当作平台 report task 使用。
"""

import csv
import io
import json
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import business_api_client as sdk  # type: ignore[import-untyped]

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import CallEvidence, McpBusinessResponse
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.reporting import (
    ReportOperations,
    ReportPage,
    ReportQuery,
    ReportRow,
    ReportTask,
)
from app.integrations.tiktok.official.accounts import OfficialReadRequests, RequestScope
from app.modules.reporting.contracts import report_buckets, validate_query

MAX_REPORT_FILE_BYTES = 64 * 1024 * 1024
_TIME_DIMENSIONS = {"stat_time_day", "stat_time_hour"}
_IDENTITY_BY_LEVEL = {
    "advertiser_id": "AUCTION_ADVERTISER",
    "campaign_id": "AUCTION_CAMPAIGN",
    "adgroup_id": "AUCTION_ADGROUP",
    "ad_id": "AUCTION_AD",
    "ad_id_v2": "AUCTION_AD",
}
_MATERIAL_FILTER = {
    "campaign_id": "campaign_ids",
    "adgroup_id": "adgroup_ids",
    "smart_plus_ad_id": "smart_plus_ad_ids",
    "main_material_id": "main_material_ids",
}
_ATTRIBUTE_KEYS = {
    "advertiser_id",
    "campaign_id",
    "campaign_name",
    "adgroup_id",
    "adgroup_name",
    "ad_id",
    "ad_name",
    "ad_id_v2",
    "smart_plus_ad_id",
    "main_material_id",
    "main_material_type",
    "ad_material_id",
    "smart_plus_creative_id",
    "smart_plus_creative_name",
}


def _error(code: str, message: str = "报表回执无效") -> DomainError:
    return DomainError(code, message)


def _data(response: Any) -> tuple[dict[str, Any], CallEvidence]:
    """取得已通过 gateway envelope 校验的数据，不接受隐式对象转换。"""
    if isinstance(response, McpBusinessResponse):
        value, evidence = response.data, response.evidence
    elif isinstance(response, dict):
        if set(response) != {"data", "request_id"} or not isinstance(response["data"], dict):
            raise _error("report_response_invalid")
        if response["request_id"] is not None and type(response["request_id"]) is not str:
            raise _error("report_response_invalid")
        value = response["data"]
        evidence = CallEvidence(request_id=response.get("request_id"))
    else:
        raw = response.to_dict() if callable(getattr(response, "to_dict", None)) else None
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
            raise _error("report_response_invalid")
        value = raw["data"]
        evidence = CallEvidence(request_id=raw.get("request_id"))
    if not isinstance(value, dict):
        raise _error("report_response_invalid")
    return value, evidence


def _identifier(value: Any) -> str:
    if type(value) is int:
        value = str(value)
    if type(value) is not str or not value.strip() or any(c.isspace() for c in value):
        raise _error("report_row_invalid", "报表身份字段无效")
    return value


def _decimal(value: Any) -> tuple[Decimal | None, str]:
    # 平台对无意义或无消耗值返回 '-'；保留不可用状态，不把它改成 0。
    if value is None or value == "-":
        return None, "MISSING"
    if type(value) not in (str, int, Decimal):
        raise _error("report_value_invalid", "报表指标必须是定点数字文本")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _error("report_value_invalid", "报表指标不是有效定点数") from exc
    if not amount.is_finite():
        raise _error("report_value_invalid", "报表指标不能是无穷值")
    return amount, "AVAILABLE"


def _bucket(query: ReportQuery, dimensions: dict[str, Any]) -> tuple[datetime, datetime]:
    if query.granularity == "RANGE":
        return report_buckets(query)[0]
    key = "stat_time_hour" if query.granularity == "HOUR" else "stat_time_day"
    raw = dimensions.get(key)
    if not isinstance(raw, str):
        # 某些 CSV 文件不带时间列；只有单桶查询才可依据请求补齐。
        buckets = report_buckets(query)
        if len(buckets) == 1:
            return buckets[0]
        raise _error("report_row_invalid", "报表行缺少时间维度")
    try:
        format_string = "%Y-%m-%d" if query.granularity == "DAY" and len(raw) == 10 else "%Y-%m-%d %H:%M:%S"
        parsed = datetime.strptime(raw, format_string).replace(
            tzinfo=ZoneInfo(query.timezone)
        ).astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise _error("report_row_invalid", "报表时间维度无效") from exc
    end = parsed + (timedelta(hours=1) if query.granularity == "HOUR" else timedelta(days=1))
    return parsed, end


def _row(query: ReportQuery, raw: dict[str, Any]) -> ReportRow:
    dimensions = raw.get("dimensions")
    metrics = raw.get("metrics")
    if type(dimensions) is not dict or type(metrics) is not dict:
        raise _error("report_row_invalid")
    contract = query.report_contract
    subject: tuple[str, ...]
    if contract.startswith("material_"):
        first = query.dimensions[0]
        material_id = _identifier(dimensions.get("main_material_id"))
        material_type = _identifier(dimensions.get("main_material_type"))
        # A5 将 typed 素材身份固定为 (material, grouping dimension,
        # main_material_id, main_material_type)，不能把素材 ID 重复填入 type 槽。
        subject = ("material", first, material_id, material_type)
        attributes: dict[str, Any] = {
            "main_material_id": material_id,
            "main_material_type": material_type,
        }
        if first != "main_material_id":
            attributes[first] = _identifier(dimensions.get(first))
    else:
        identity = query.dimensions[0]
        subject = ("account" if identity == "advertiser_id" else "campaign" if identity == "campaign_id" else "adgroup" if identity == "adgroup_id" else "creative" if contract == "basic_smart_plus_creative" else "ad", _identifier(dimensions.get(identity)))
        attributes = {}
    # 只携带事实层已列入白名单的描述/关联字段；素材与创意的数组身份
    # 原样保留，绝不按数组长度拆分或平均分摊 spend。
    for key, value in dimensions.items():
        if key not in _ATTRIBUTE_KEYS or key in {"main_material_id", "main_material_type"}:
            continue
        if key == "smart_plus_creative_id":
            if type(value) is list and all(type(item) is str for item in value):
                attributes[key] = list(value)
        elif type(value) is str:
            attributes[key] = value
    values: dict[str, Decimal | None] = {}
    availability: dict[str, str] = {}
    for metric in query.metrics:
        amount, state = _decimal(metrics.get(metric))
        values[metric], availability[metric] = amount, state
    start, end = _bucket(query, dimensions)
    return ReportRow(subject, start, end, values, availability, attributes)


def _page(query: ReportQuery, response: Any, seen: set[int]) -> ReportPage:
    data, evidence = _data(response)
    rows = data.get("list")
    info = data.get("page_info")
    if type(rows) is not list or type(info) is not dict or any(type(row) is not dict for row in rows):
        raise _error("report_response_invalid")
    page = info.get("page", query.page)
    page_size = info.get("page_size")
    total_page = info.get("total_page")
    total_number = info.get("total_number")
    if any(type(value) is not int for value in (page, page_size, total_page, total_number)):
        raise _error("report_response_invalid")
    page = cast(int, page)
    page_size = cast(int, page_size)
    total_page = cast(int, total_page)
    total_number = cast(int, total_number)
    if page != query.page or page in seen or page < 1 or page_size < 1 or total_page < 0 or total_number < 0:
        raise _error("report_page_repeated", "报表页号重复或与请求不一致")
    seen.add(page)
    expected_pages = max(1, (total_number + page_size - 1) // page_size)
    expected_rows = 0 if total_number == 0 else min(page_size, max(0, total_number - (page - 1) * page_size))
    if total_page not in ({0, 1} if total_number == 0 else {expected_pages}) or len(rows) != expected_rows:
        raise _error("report_response_invalid")
    next_page = page + 1 if page < expected_pages else None
    parsed = tuple(_row(query, row) for row in rows)
    return ReportPage(parsed, next_page, next_page is None, evidence, page=page)


def _filter(query: ReportQuery) -> list[dict[str, str]] | None:
    if not query.filter_ids:
        return None
    identity = query.dimensions[0]
    field = _MATERIAL_FILTER.get(identity, "ad_ids" if identity in {"ad_id", "ad_id_v2"} else f"{identity}s")
    if identity == "ad_id_v2":
        field = "ad_id_v2"
    return [{"field_name": field, "filter_type": "IN", "filter_value": json.dumps(list(query.filter_ids))}]


def _payload(
    query: ReportQuery, *, page_size: int = 1000, ad_type: str | None = None
) -> dict[str, Any]:
    if query.report_contract == "material_overview":
        if any(dim in _TIME_DIMENSIONS for dim in query.dimensions):
            raise _error("report_query_invalid", "素材 overview 不支持时间拆分")
        return {
            "advertiser_id": query.advertiser_id,
            "dimensions": list(query.dimensions),
            "metrics": list(query.metrics),
            "start_date": query.start_date.isoformat(),
            "end_date": query.end_date.isoformat(),
            "page": query.page,
            "page_size": min(page_size, 100),
            **({"filtering": _material_filter(query)} if _material_filter(query) else {}),
        }
    if query.report_contract == "material_breakdown":
        return {
            "advertiser_id": query.advertiser_id,
            "dimensions": list(query.dimensions),
            "metrics": list(query.metrics),
            "start_date": query.start_date.isoformat(),
            "end_date": query.end_date.isoformat(),
            "page": query.page,
            "page_size": min(page_size, 100),
        }
    identity = query.dimensions[0]
    payload = {
        "report_type": "BASIC",
        "service_type": "AUCTION",
        "advertiser_id": query.advertiser_id,
        "data_level": _IDENTITY_BY_LEVEL[identity],
        "dimensions": list(query.dimensions),
        "metrics": list(query.metrics),
        "start_date": query.start_date.isoformat(),
        "end_date": query.end_date.isoformat(),
        "page": query.page,
        "page_size": min(page_size, 1000),
        **({"filtering": _filter(query)} if _filter(query) else {}),
    }
    if ad_type is not None:
        type_filter = {
            "REGULAR": "MANUAL",
            "LEGACY_SMART_PLUS": "SMART_PLUS",
            "SMART_PLUS": "UPGRADED_SMART_PLUS",
        }.get(ad_type)
        if type_filter is None:
            raise _error("report_query_invalid", "广告类型未核验")
        if query.report_contract == "basic_smart_plus_creative":
            type_filter = "UPGRADED_SMART_PLUS_CREATIVE"
        filters_value = payload.setdefault("filtering", [])
        filters: list[dict[str, str]]
        if isinstance(filters_value, list):
            filters = cast(list[dict[str, str]], filters_value)
        else:
            filters = []
            payload["filtering"] = filters
        filters.append(
            {
                "field_name": "campaign_automation_type",
                "filter_type": "IN",
                "filter_value": json.dumps([type_filter]),
            }
        )
    return payload


def _async_payload(query: ReportQuery, *, ad_type: str | None = None) -> dict[str, Any]:
    if ad_type is not None and query.report_contract in {
        "basic_ad",
        "basic_smart_plus_ad",
        "basic_smart_plus_creative",
    }:
        # 异步 allowlist 没有已核验的 campaign_automation_type 过滤器；
        # 拒绝混合类型任务，不能把不同广告类型塞进同一事实分区。
        raise _error("report_async_ad_type_unsupported", "异步报表无法安全隔离广告类型")
    payload = _payload(query, ad_type=ad_type)
    # 平台 task/create 没有同步 page/page_size；保留完整 query 口径，其余字段由
    # 任务合同决定，避免把本地分页参数发送到生成 SDK/MCP schema。
    payload.pop("page", None)
    payload.pop("page_size", None)
    payload["enable_report_title_translation"] = False
    return payload


def _material_filter(query: ReportQuery) -> dict[str, list[str]] | None:
    if not query.filter_ids:
        return None
    identity = query.dimensions[0]
    if identity not in _MATERIAL_FILTER:
        raise _error("report_query_invalid", "素材过滤器维度未核验")
    return {_MATERIAL_FILTER[identity]: list(query.filter_ids)}


def _csv_rows(payload: bytes, query: ReportQuery) -> ReportPage:
    if len(payload) > MAX_REPORT_FILE_BYTES:
        raise _error("report_file_too_large")
    try:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("missing header")
        rows = []
        for item in reader:
            dimensions: dict[str, Any] = {}
            metrics: dict[str, Any] = {}
            normalized = {str(key).strip().lower().replace(" ", "_"): value for key, value in item.items() if key}
            for dimension in query.dimensions:
                dimensions[dimension] = normalized.get(dimension) or normalized.get(dimension.replace("_", " "))
            for metric in query.metrics:
                metrics[metric] = normalized.get(metric) or normalized.get(metric.replace("_", " "))
            rows.append(_row(query, {"dimensions": dimensions, "metrics": metrics}))
        return ReportPage(tuple(rows), None, True, CallEvidence(), page=1)
    except UnicodeDecodeError as exc:
        raise _error("report_file_invalid", "报表文件编码不受支持") from exc
    except csv.Error as exc:
        raise _error("report_file_invalid") from exc
    except ValueError as exc:
        raise _error("report_file_invalid") from exc


class SdkReportingOperations(ReportOperations):
    """绑定官方 SDK 的同步/异步报表操作。"""

    def __init__(
        self,
        client: Any,
        *,
        route: FrozenTikTokRoute,
        request_scope: RequestScope,
        deadline: datetime,
        ad_type: str | None = None,
        file_fetcher: Callable[[str, datetime], bytes] | None = None,
    ) -> None:
        self.route = route
        self._requests = OfficialReadRequests(client, request_scope=request_scope, deadline=deadline)
        self._deadline = deadline
        self._ad_type = ad_type
        self._seen: dict[tuple[Any, ...], set[int]] = {}
        self._fetcher = file_fetcher or self._fetch_scoped_file
        self._request_scope = request_scope
        self._download_advertiser: str | None = None

    def _fetch_scoped_file(self, url: str, deadline: datetime) -> bytes:
        # 签名文件是第二个物理 HTTP 请求，也必须重新经过同一租约、权限和额度门禁。
        if self._download_advertiser is None:
            raise _error("report_file_invalid", "报表下载账户未绑定")
        with self._request_scope(self._download_advertiser, "reports.task_download", deadline):
            return self._fetch_file(url, deadline)

    def _validate(self, query: ReportQuery) -> None:
        try:
            validate_query(query, channel=self.route.channel, ad_type=self._ad_type)
        except (TypeError, ValueError) as exc:
            raise _error("report_query_invalid", "报表维度或指标未核验") from exc

    def _call(self, operation: str, arguments: dict[str, Any]) -> McpBusinessResponse:
        client = self._requests.client
        token = client.default_headers["Access-Token"]
        api = sdk.ReportingApi(client)
        if operation == "reports.integrated":
            return self._requests.invoke(operation, arguments["advertiser_id"], lambda timeout: api.report_integrated_get(arguments.pop("report_type"), token, **{**arguments, "_request_timeout": timeout}), exact_numbers=True)
        if operation == "reports.material_overview":
            return self._requests.invoke(operation, arguments["advertiser_id"], lambda timeout: api.smart_plus_material_report_overview(arguments.pop("advertiser_id"), arguments.pop("dimensions"), token, **{**arguments, "_request_timeout": timeout}), exact_numbers=True)
        if operation == "reports.material_breakdown":
            return self._requests.invoke(operation, arguments["advertiser_id"], lambda timeout: api.smart_plus_material_report_breakdown(arguments.pop("advertiser_id"), arguments.pop("dimensions"), arguments.pop("start_date"), arguments.pop("end_date"), token, **{**arguments, "_request_timeout": timeout}), exact_numbers=True)
        raise _error("report_operation_invalid")

    def read_page(self, query: ReportQuery) -> ReportPage:
        self._validate(query)
        key = (query.advertiser_id, query.report_contract, query.filter_ids, query.page,
               query.dimensions, query.metrics, query.start_date, query.end_date,
               query.granularity, query.currency, query.timezone, query.attribution,
               self._ad_type)
        seen = self._seen.setdefault(key[:-1], set())
        args = _payload(query, ad_type=self._ad_type)
        operation = {"material_overview": "reports.material_overview", "material_breakdown": "reports.material_breakdown"}.get(query.report_contract, "reports.integrated")
        return _page(query, self._call(operation, args), seen)

    def create_task(self, query: ReportQuery) -> ReportTask:
        self._validate(query)
        if query.report_contract.startswith("material_"):
            raise _error("report_async_unsupported", "素材报表异步维度未核验")
        body = _async_payload(query, ad_type=self._ad_type)
        body.update({"report_type": "BASIC", "output_format": "CSV_DOWNLOAD"})
        # SDK body models intentionally remain at this transport boundary; no async_req.
        model = sdk.ReportTaskCreateBody(**body)
        token = self._requests.client.default_headers["Access-Token"]
        result = self._requests.invoke("reports.task_create", query.advertiser_id, lambda timeout: sdk.ReportingApi(self._requests.client).report_task_create(token, body=model, _request_timeout=timeout), exact_numbers=True)
        data, _ = _data(result)
        task_id = _identifier(data.get("task_id"))
        return ReportTask(task_id, query.advertiser_id, "PENDING", query)

    def check_task(self, task: ReportTask) -> ReportTask:
        if task.status == "READY":
            return task
        token = self._requests.client.default_headers["Access-Token"]
        result = self._requests.invoke("reports.task_check", task.advertiser_id, lambda timeout: sdk.ReportingApi(self._requests.client).report_task_check(task.task_id, task.advertiser_id, token, _request_timeout=timeout), exact_numbers=True)
        data, _ = _data(result)
        status = data.get("status")
        mapped = {"QUEUING": "PENDING", "PROCESSING": "RUNNING", "SUCCESS": "READY", "FAILED": "FAILED", "CANCELED": "FAILED"}.get(cast(str, status))
        if mapped is None:
            raise _error("report_task_status_unknown", "异步报表状态未核验")
        return ReportTask(task.task_id, task.advertiser_id, cast(Literal["PENDING", "RUNNING", "READY", "FAILED"], mapped), task.query)

    def download_task(self, task: ReportTask) -> ReportPage:
        if task.status != "READY":
            raise _error("report_task_not_ready", "异步报表尚未 READY")
        token = self._requests.client.default_headers["Access-Token"]
        api = sdk.ReportingApi(self._requests.client)
        result = self._requests.invoke("reports.task_download", task.advertiser_id, lambda timeout: self._download_metadata(api, token, task, timeout), exact_numbers=True)
        data, evidence = _data(result)
        if isinstance(data.get("rows"), list):
            row_count = len(data["rows"])
            response = {"data": {"list": data["rows"], "page_info": {"page": 1, "page_size": max(1, row_count), "total_page": 1, "total_number": row_count}}, "request_id": evidence.request_id}
            return _page(task.query, response, set())
        url = data.get("download_url")
        output_format = data.get("output_format")
        if output_format not in {"CSV_DOWNLOAD", "CSV_STRING", "XLSX_DOWNLOAD"}:
            raise _error("report_file_invalid", "异步报表文件格式未核验")
        if output_format == "XLSX_DOWNLOAD":
            raise _error("report_file_unsupported", "XLSX 报表解析合同未核验")
        if type(url) is not str or not url.startswith("https://"):
            raise _error("report_file_invalid", "异步报表下载地址缺失")
        self._download_advertiser = task.advertiser_id
        try:
            return _csv_rows(self._fetcher(url, self._deadline), task.query)
        finally:
            self._download_advertiser = None

    @staticmethod
    def _download_metadata(api: Any, token: str, task: ReportTask, timeout: tuple[float, float]) -> Any:
        # 生成 SDK 没有该方法，使用同一 ApiClient 发送严格的 metadata 请求。
        return api.api_client.call_api("/open_api/v1.3/report/task/download/", "GET", {}, [("advertiser_id", task.advertiser_id), ("task_id", task.task_id)], {"Access-Token": token}, response_type="InlineResponse200", auth_settings=[], _return_http_data_only=True, _request_timeout=timeout)

    @staticmethod
    def _fetch_file(url: str, deadline: datetime) -> bytes:
        remaining = max(0.1, (deadline - datetime.now(UTC)).total_seconds())
        request = urllib.request.Request(url, method="GET")
        chunks: list[bytes] = []
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=min(30.0, remaining)) as response:
                expected = response.headers.get("Content-Length")
                expected_length = int(expected) if expected and expected.isdigit() else None
                if expected_length is None:
                    raise _error("report_file_integrity_missing", "报表文件缺少完整性长度证据")
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_REPORT_FILE_BYTES:
                        raise _error("report_file_too_large")
                    chunks.append(chunk)
                if expected_length is not None and total != expected_length:
                    raise _error("report_file_incomplete", "报表文件未完整下载")
        except DomainError:
            raise
        except Exception as exc:
            raise _error("report_file_download_failed") from exc
        return b"".join(chunks)


# 兼容调用方的命名约定。
SdkReportOperations = SdkReportingOperations
SdkReportingAdapter = SdkReportingOperations


def plan_report_shards(query: ReportQuery, *, entity_ids: tuple[str, ...], max_ids: int) -> tuple[ReportQuery, ...]:
    """按平台过滤器上限切片；分页只用于单个过滤器范围内的结果页。"""
    if type(max_ids) is not int or max_ids < 1 or any(type(value) is not str or not value for value in entity_ids):
        raise ValueError("invalid report shard arguments")
    if not entity_ids:
        return (query,)
    return tuple(
        ReportQuery(
            advertiser_id=query.advertiser_id,
            report_contract=query.report_contract,
            metric_family=query.metric_family,
            dimensions=query.dimensions,
            metrics=query.metrics,
            start_date=query.start_date,
            end_date=query.end_date,
            granularity=query.granularity,
            currency=query.currency,
            timezone=query.timezone,
            attribution=query.attribution,
            filter_ids=tuple(entity_ids[index : index + max_ids]),
            page=1,
        )
        for index in range(0, len(entity_ids), max_ids)
    )
