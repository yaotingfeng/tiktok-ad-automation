"""已归档官方回执合同：PROCESSING / SUCCESS，下载必须带成功状态和正文。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.errors import DomainError


class SdkChangeLogReader:
    def __init__(
        self, *, send: Callable[[str, str, dict[str, Any]], dict[str, Any]]
    ) -> None:
        self._send = send

    @property
    def request_count(self) -> int:
        return getattr(self._send, "request_count", 0)

    def change_log_create(
        self, *, advertiser_id: str, start: datetime, end: datetime
    ) -> str:
        if (
            start.utcoffset() is None
            or end.utcoffset() is None
            or not timedelta(0) < end - start <= timedelta(days=30)
        ):
            raise DomainError("operation_log_range_invalid", "日志查询跨度无效")
        data = self._send(
            "changelog.create",
            advertiser_id,
            {
                "advertiser_id": advertiser_id,
                "start_time": start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": end.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                "timezone": "UTC",
            },
        )
        if (
            not isinstance(data.get("task_id"), str)
            or not 0 < len(data["task_id"]) <= 128
        ):
            raise DomainError("operation_log_response_invalid", "日志导出任务回执无效")
        return str(data["task_id"])

    def change_log_check(self, *, advertiser_id: str, task_id: str) -> str:
        data = self._send(
            "changelog.check",
            advertiser_id,
            {"advertiser_id": advertiser_id, "task_id": task_id},
        )
        status = data.get("status")
        if status not in {"PROCESSING", "SUCCESS"}:
            raise DomainError(
                "operation_log_task_unavailable",
                "平台日志任务不可用，请重建导出",
                retryable=True,
            )
        return str(status)

    def change_log_download(self, *, advertiser_id: str, task_id: str) -> str:
        data = self._send(
            "changelog.download",
            advertiser_id,
            {"advertiser_id": advertiser_id, "task_id": task_id},
        )
        if (
            data.get("status") != "SUCCESS"
            or not isinstance(data.get("changelog"), str)
            or not data["changelog"]
        ):
            raise DomainError(
                "operation_log_response_invalid", "日志下载未成功或缺少正文"
            )
        return str(data["changelog"])
