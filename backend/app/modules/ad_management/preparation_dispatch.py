"""准备检查点的事务恢复消息；同一未来时间桶合并，避免每块产生一个任务。"""

from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any

from app.modules.ad_management.models import ManagementTask

PREPARATION_WATCHDOG_DELAY = timedelta(seconds=120)
PREPARATION_WATCHDOG_BUCKET_SECONDS = 30


def preparation_watchdog_entry(
    task: ManagementTask, *, now: datetime | None = None
) -> tuple[str, str, dict[str, Any], datetime]:
    from app.modules.ad_management.tasks import MANAGEMENT_DISPATCH_TASK

    # 向上取整确保消息至少在120秒后才可投递；快速连续的200项检查点
    # 共用一个30秒桶。旧桶即使已经发布，新检查点也会保留新的未来恢复消息。
    due = (now or datetime.now(UTC)) + PREPARATION_WATCHDOG_DELAY
    bucket = ceil(due.timestamp() / PREPARATION_WATCHDOG_BUCKET_SECONDS)
    available_at = datetime.fromtimestamp(
        bucket * PREPARATION_WATCHDOG_BUCKET_SECONDS, UTC
    )
    return (
        MANAGEMENT_DISPATCH_TASK,
        f"ad-management-prepare-watch:{task.id}:{bucket}",
        {"task_id": str(task.id), "generation": 1},
        available_at,
    )
