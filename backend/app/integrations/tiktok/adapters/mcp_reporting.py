"""官方 TikTok for Business MCP 报表适配器。"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Literal, cast

from app.integrations.tiktok.adapters.sdk_reporting import (
    SdkReportingOperations,
    _async_payload,
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
        deadline: datetime | None = None,
        file_fetcher: Callable[[str, datetime], bytes] | None = None,
        download_gate: Callable[[str, datetime], AbstractContextManager[None]] | None = None,
    ) -> None:
        self._client = client
        self.route = route
        self._ad_type = ad_type
        self._deadline = deadline
        self._download_gate = download_gate
        self._fetcher = file_fetcher or self._fetch_scoped_file
        self._download_advertiser: str | None = None
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
        key = (
            query.advertiser_id,
            query.report_contract,
            query.filter_ids,
            query.dimensions,
            query.metrics,
            query.start_date,
            query.end_date,
            query.granularity,
            query.currency,
            query.timezone,
            query.attribution,
            self._ad_type,
        )
        seen = self._seen.setdefault(key, set())
        # The gateway instance may serve more than one persisted sync run with
        # the same query. A new first page starts a new pagination sequence;
        # otherwise a page 1 seen by the previous run is incorrectly rejected
        # as a cross-run page conflict.
        if query.page == 1:
            seen.clear()
        operation = {
            "material_overview": "reports.material_overview",
            "material_breakdown": "reports.material_breakdown",
        }.get(query.report_contract, "reports.integrated")
        return _page(query, self._call(operation, _payload(query, ad_type=self._ad_type)), seen)

    def create_task(self, query: ReportQuery) -> ReportTask:
        self._validate(query)
        if query.report_contract.startswith("material_"):
            raise _error("report_async_unsupported", "素材报表异步维度未核验")
        payload = _async_payload(query, ad_type=self._ad_type)
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
            row_count = len(data["rows"])
            page = {"data": {"list": data["rows"], "page_info": {"page": 1, "page_size": max(1, row_count), "total_page": 1, "total_number": row_count}}, "request_id": evidence.request_id}
            return _page(task.query, page, set())
        url = data.get("download_url")
        if type(url) is not str or not url.startswith("https://") or self._fetcher is None:
            raise _error("report_file_invalid", "异步报表下载文件缺失")
        self._download_advertiser = task.advertiser_id
        try:
            deadline = self._deadline
            if deadline is None:
                raise _error("report_deadline_invalid", "报表任务期限未冻结")
            return _csv_rows(self._fetcher(url, deadline), task.query)
        finally:
            self._download_advertiser = None

    def _fetch_scoped_file(self, url: str, deadline: datetime) -> bytes:
        if self._download_advertiser is None or self._download_gate is None:
            raise _error("report_download_gate_required", "MCP 文件下载缺少冻结门禁")
        with self._download_gate(self._download_advertiser, deadline):
            return SdkReportingOperations._fetch_file(url, deadline)


McpReportOperations = McpReportingOperations
McpReportingAdapter = McpReportingOperations
