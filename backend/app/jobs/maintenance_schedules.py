"""维护入口不增加 Beat 调度；复用 celery_app 中已有的有界清理任务。"""

# reporting.cleanup_store 已每分钟运行；jobs.compact_dispatches 已定期运行。
# 两者分别由 ads-reporting/control 执行，不能另加无界清理拖慢业务调度。
MAINTENANCE_TASK_NAMES = ("reporting.cleanup_store", "jobs.compact_dispatches")
