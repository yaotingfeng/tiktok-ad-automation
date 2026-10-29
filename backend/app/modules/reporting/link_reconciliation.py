"""归并维护的短期轮转状态；业务事实始终以 PostgreSQL 提交为准。"""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlmodel import Session

from app.modules.reporting import link_work

CURSOR_KEY = "reporting:link-reconciliation:cursor:v1"
LOCK_KEY = "reporting:link-reconciliation:lock:v1"


def reconcile_link_work_batch(*, database_engine, redis_client, now, limit=50):
    # 租约长于一秒维护预算；并发 Beat 不交错覆盖位置，崩溃后自动释放。
    lock = redis_client.lock(LOCK_KEY, timeout=30, blocking=False)
    if not lock.acquire(blocking=False):
        return 0
    try:
        raw = redis_client.get(CURSOR_KEY)
        scan = {"cursor": json.loads(raw) if raw is not None else None}
        with Session(database_engine) as session, session.begin():
            session.connection().execute(text("SET LOCAL statement_timeout='1s'"))
            session.connection().execute(text("SET LOCAL lock_timeout='100ms'"))
            count = link_work.reconcile_link_work(
                session,
                now=now,
                limit=limit,
                scan=scan,
                deadline=datetime.now(UTC) + timedelta(seconds=1),
            )
        # 必须先提交业务变更；Redis 失败或丢失只能安全重扫，不能跳过回滚任务。
        redis_client.set(CURSOR_KEY, json.dumps(scan["cursor"]), ex=7 * 86400)
        return count
    finally:
        lock.release()
