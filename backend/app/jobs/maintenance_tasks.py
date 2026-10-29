"""人工维护入口复用已注册的有界任务，不再额外注册全表删除任务。"""

from app.jobs.tasks import compact_dispatches_task
from app.modules.reporting.job_tasks import cleanup_store_task


def maintain_process_data() -> dict:
    """各自短事务提交；失败保留已完成进度，重复调用仍受现有游标和锁保护。"""
    return {
        "cleanup": cleanup_store_task.run(),
        "dispatch_compacted": compact_dispatches_task.run(limit=1000),
    }
