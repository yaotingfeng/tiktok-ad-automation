"""官方 TikTok for Business MCP 报表适配器。"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast

from app.integrations.tiktok.adapters.sdk_reporting import (
    _csv_rows,
    _data,
    _error,
    _identifier,
    _page,
    _payload,
)
from app.integrations.tiktok.contracts.common import McpBusinessResponse
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.reporting import (
    ReportOperations,
    ReportPage,
    ReportQuery,
    ReportTask,
)
from app.integrations.tiktok.mcp.transport import BoundMCPClient
from app.modules.reporting.contracts import validate_query


class McpReportingOperations(ReportOperations):
    def __init__(
        self,
        client: BoundMCPClient,
        *,
        route: FrozenTikTokRoute,
        ad_type: str | None = None,
        file_fetcher: Callable[[str, datetime], bytes] | None = None,
    ) -> None:
        self._client = client
        self.route = route
        self._ad_type = ad_type
        from app.integrations.tiktok.adapters.sdk_reporting import (
            SdkReportingOperations,
        )

        self._fetcher = file_fetcher or SdkReportingOperations._fetch_file
        self._seen: dict[tuple[Any, ...], set[int]] = {}

    def _validate(self, query: ReportQuery) -> None:
        try:
            validate_query(query, channel=self.route.channel, ad_type=self._ad_type)
        except (TypeError, ValueError) as exc:
            raise _error("report_query_invalid", "报表维度或指标未核验") from exc

    def _call(self, operation: str, arguments: dict[str, Any]) -> McpBusinessResponse:
        return self._client.call(
            operation=operation,
            advertiser_id=arguments.get("advertiser_id"),
            arguments=arguments,
        )

    def read_page(self, query: ReportQuery) -> ReportPage:
        self._validate(query)
        key = (query.advertiser_id, query.report_contract)
        seen = self._seen.setdefault(key, set())
        operation = {
            "material_overview": "reports.material_overview",
            "material_breakdown": "reports.material_breakdown",
        }.get(query.report_contract, "reports.integrated")
        return _page(query, self._call(operation, _payload(query)), seen)

    def create_task(self, query: ReportQuery) -> ReportTask:
        self._validate(query)
        if query.report_contract.startswith("material_"):
            raise _error("report_async_unsupported", "素材报表异步维度未核验")
        payload = _payload(query)
        payload.update({"report_type": "BASIC", "output_format": "CSV_DOWNLOAD"})
        receipt = self._client.call(
            operation="reports.task_create",
            advertiser_id=query.advertiser_id,
            arguments=payload,
        )
        data, _ = _data(receipt)
        return ReportTask(_identifier(data.get("task_id")), query.advertiser_id, "PENDING", query)

    def check_task(self, task: ReportTask) -> ReportTask:
        if task.status == "READY":
            return task
        receipt = self._client.call(
            operation="reports.task_check",
            advertiser_id=task.advertiser_id,
            arguments={"advertiser_id": task.advertiser_id, "task_id": task.task_id},
        )
        data, _ = _data(receipt)
        mapped = {"QUEUING": "PENDING", "PROCESSING": "RUNNING", "SUCCESS": "READY", "FAILED": "FAILED", "CANCELED": "FAILED"}.get(cast(str, data.get("status")))
        if mapped is None:
            raise _error("report_task_status_unknown", "异步报表状态未核验")
        return ReportTask(task.task_id, task.advertiser_id, cast(Literal["PENDING", "RUNNING", "READY", "FAILED"], mapped), task.query)

    def download_task(self, task: ReportTask) -> ReportPage:
        if task.status != "READY":
            raise _error("report_task_not_ready", "异步报表尚未 READY")
        receipt = self._client.call(
            operation="reports.task_download",
            advertiser_id=task.advertiser_id,
            arguments={"advertiser_id": task.advertiser_id, "task_id": task.task_id},
        )
        data, evidence = _data(receipt)
        if isinstance(data.get("rows"), list):
            page = {"data": {"list": data["rows"], "page_info": {"page": 1, "page_size": len(data["rows"]), "total_page": 1, "total_number": len(data["rows"])}}, "request_id": evidence.request_id}
            return _page(task.query, page, set())
        url = data.get("download_url")
        if type(url) is not str or not url.startswith("https://") or self._fetcher is None:
            raise _error("report_file_invalid", "异步报表下载文件缺失")
        return _csv_rows(self._fetcher(url, datetime.now(UTC)), task.query)


McpReportOperations = McpReportingOperations
McpReportingAdapter = McpReportingOperations
