"""旧投递通过正式任务交接到当前路由，不在错误的保留槽执行慢准备。"""

from typing import Any

from app.jobs.tasks import dispatch_queue


def ensure_dispatch_queue(task: Any) -> None:
    expected = dispatch_queue(task.name)
    delivered = (task.request.delivery_info or {}).get("routing_key")
    if delivered and delivered != expected:
        # Celery retry保留task_id、args和kwargs。交接失败不产生业务效果，
        # 同一任务重复投递仍由原job/revision/claim与持久outbox恢复保护。
        raise task.retry(queue=expected, countdown=0, max_retries=None)
