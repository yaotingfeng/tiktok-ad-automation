"""控制面数据库总预算，逐语句递减并保留外层事务恢复能力。"""

from contextlib import contextmanager
from time import monotonic

from sqlalchemy import event, text


class QueryBudgetExceeded(Exception):
    """规划任务主动让出，避免Celery软超时打断事务回滚。"""


@contextmanager
def query_budget(session, *, seconds):
    """每条SQL都受剩余总预算约束；不放宽调用方已有的单语句期限。"""
    connection = session.connection()
    previous = int(
        connection.execute(
            text("SELECT setting FROM pg_settings WHERE name='statement_timeout'")
        ).scalar_one()
    )
    deadline = monotonic() + seconds
    configured = previous

    def before(_conn, cursor, _sql, _params, _context, _many):
        nonlocal configured
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise QueryBudgetExceeded()
        # 按50ms刻度下调，避免每次微小时间变化都多发SET；尾段最多50ms余量。
        timeout = max(50, int(remaining * 20) * 50)
        if previous:
            timeout = min(timeout, previous)
        if timeout != configured:
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)", (f"{timeout}ms",)
            )
            configured = timeout

    completed = False
    event.listen(connection, "before_cursor_execute", before)
    try:
        yield
        completed = True
    finally:
        # 必须先卸载监听，再由外层SAVEPOINT回滚，不能用过期预算阻止恢复事务。
        event.remove(connection, "before_cursor_execute", before)
        if completed and configured != previous:
            connection.execute(
                text("SELECT set_config('statement_timeout', :value, true)"),
                {"value": f"{previous}ms"},
            )
