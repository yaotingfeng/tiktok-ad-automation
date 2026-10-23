"""目录发现任务（官方 API 授权发现、MCP 发现、运行目录再观察）的统一兜底。

每一步只靠一条派发消息推进：消息在 broker 中丢失、被即将停止的工作进程取走，
或工作进程在领取前出错（数据库/Redis 暂不可用），任务都会停在 RUNNING /
ADMISSION_WAIT 且再也没有消息，界面永远显示“正在发现”。这里不追究单条消息的
去向：Beat 定期找出“已到期却超过宽限期无人领取”的任务重新派发；重复派发由
各消费者的代数与 claim 校验去重，最多多一次空跑。

本地资源（数据库/Redis 短期期限）反复失败也不能无限重试：按次数指数退避，
超过上限落为 ERROR，界面显示发现失败，由管理员重新授权或重新同步。
"""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from app.jobs.celery_app import celery_app

from .models import DiscoveryRun, TikTokConnection

# 领取期限 60 秒、任务硬超时 45 秒；超过两分钟仍无人领取才视为丢失。
REPAIR_GRACE_SECONDS = 120
LOCAL_RESOURCE_ERROR = "tiktok_local_resources_unavailable"
# 5 秒起翻倍、单次最多 5 分钟；12 次累计约 35 分钟，足够覆盖数据库/Redis 短时故障。
LOCAL_RETRY_LIMIT = 12
_LOCAL_RETRY_BASE_MS = 5000
_LOCAL_RETRY_MAX_MS = 300_000
_ACTIVE = ("RUNNING", "ADMISSION_WAIT")


def local_retry_delay_ms(run: DiscoveryRun) -> int | None:
    """登记一次本地资源失败并返回退避毫秒；超过上限返回 None，调用者落 ERROR。

    计数存于 work，同一事务随任务状态一起提交；任一步成功推进时清零。
    """
    work = copy.deepcopy(run.work)
    failures = int(work.get("local_failures", 0)) + 1
    work["local_failures"] = failures
    run.work = work
    if failures > LOCAL_RETRY_LIMIT:
        return None
    return min(_LOCAL_RETRY_MAX_MS, _LOCAL_RETRY_BASE_MS * 2 ** (failures - 1))


def clear_local_failures(work: dict[str, Any]) -> None:
    """成功推进一步后清零，避免早期偶发失败累计到后续阶段。"""
    work.pop("local_failures", None)


def _requeue(session: Session, run: DiscoveryRun, *, suffix: str) -> None:
    # 延迟导入：三个消费者模块各自导入本模块的重试辅助函数。
    from .mcp_discovery_tasks import _queue as queue_mcp
    from .runtime_directory import MODE as RUNTIME_MODE
    from .runtime_directory import queue_runtime
    from .tasks import queue_run

    if run.work.get("mode") == RUNTIME_MODE:
        queue_runtime(session, run, suffix=suffix)
        return
    connection = session.get(TikTokConnection, run.connection_id)
    if connection is not None and connection.kind == "OFFICIAL_API":
        queue_run(session, run, suffix=suffix)
    else:
        queue_mcp(session, run=run, suffix=suffix)


def repair_discoveries(*, database_engine: Any, limit: int = 100) -> int:
    """重新派发到期后超过宽限期仍无人领取的发现任务；返回补派数量。"""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now = datetime.now(UTC)
    stale = now - timedelta(seconds=REPAIR_GRACE_SECONDS)
    idle_since = func.coalesce(
        col(DiscoveryRun.next_attempt_at), col(DiscoveryRun.created_at)
    )
    with Session(database_engine) as session, session.begin():
        runs = session.exec(
            select(DiscoveryRun)
            .where(
                col(DiscoveryRun.status).in_(_ACTIVE),
                # 领取未过期说明有进程在跑；过期领取由原恢复消息处理，再丢失才轮到这里。
                or_(
                    col(DiscoveryRun.claimed_until).is_(None),
                    col(DiscoveryRun.claimed_until) <= stale,
                ),
                idle_since <= stale,
            )
            .order_by(idle_since, col(DiscoveryRun.id))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for run in runs:
            # 记下补派时间，宽限期内不重复补派；消费者按 <= now 判断到期，不受影响。
            run.next_attempt_at = now
            session.add(run)
            _requeue(session, run, suffix=f"repair:{uuid4()}")
        return len(runs)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="accounts.repair_discoveries", time_limit=45, soft_time_limit=40
)
def repair_discoveries_task() -> int:
    from app.core.db import engine

    return repair_discoveries(database_engine=engine)
