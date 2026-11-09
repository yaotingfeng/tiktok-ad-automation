"""官方异步操作日志读取合同，与指标报表独立。"""

from datetime import datetime
from typing import Protocol


class ChangeLogReader(Protocol):
    def change_log_create(
        self, *, advertiser_id: str, start: datetime, end: datetime
    ) -> str: ...
    def change_log_check(self, *, advertiser_id: str, task_id: str) -> str: ...
    def change_log_download(self, *, advertiser_id: str, task_id: str) -> str: ...
