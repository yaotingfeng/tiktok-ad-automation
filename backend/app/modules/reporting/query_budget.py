"""控制面数据库总预算，逐语句递减并保留外层事务恢复能力。"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from time import monotonic

from sqlalchemy import event, text

_STATE_KEY = "tkada_query_budget"
_CONTROL_OPTION = "tkada_query_budget_control"


class QueryBudgetExceeded(Exception):
    """规划任务主动让出，避免Celery软超时打断事务回滚。"""


@dataclass
class _BudgetState:
    previous: int
    configured: int | None
    deadlines: list[float] = field(default_factory=list)

    def timeout(self, *, strict=True):
        remaining = min(self.deadlines) - monotonic()
        if strict and remaining <= 0:
            raise QueryBudgetExceeded()
        # 按50ms刻度下调；原有限单语句期限始终作为更严格上限。
        timeout = max(50, int(remaining * 20) * 50)
        return min(timeout, self.previous) if self.previous else timeout


@contextmanager
def query_budget(session, *, seconds):
    """同连接共用监听器和期限栈；子预算退出时恢复父级当前剩余期限。"""
    connection = session.connection()
    state = connection.info.get(_STATE_KEY)
    outermost = state is None
    if outermost:
        previous = int(
            connection.execute(
                text("SELECT setting FROM pg_settings WHERE name='statement_timeout'")
            ).scalar_one()
        )
        state = _BudgetState(previous=previous, configured=previous)
        connection.info[_STATE_KEY] = state

        def before(_conn, cursor, sql, _params, context, _many):
            if context.execution_options.get(_CONTROL_OPTION):
                return
            if (
                sql.lstrip()
                .upper()
                .startswith(
                    ("ROLLBACK TO SAVEPOINT ", "RELEASE SAVEPOINT ", "SAVEPOINT ")
                )
            ):
                # PG取消后事务已中止，ROLLBACK前绝不能先发SET；回滚还可能恢复
                # 之前的SET LOCAL值，下条业务SQL必须重新下发实际剩余期限。
                state.configured = None
                return
            timeout = state.timeout()
            if timeout != state.configured:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{timeout}ms",),
                )
                state.configured = timeout

        event.listen(connection, "before_cursor_execute", before)
    state.deadlines.append(monotonic() + seconds)
    completed = False
    try:
        yield
        completed = True
    finally:
        state.deadlines.pop()
        if outermost:
            # 先解除监听/私有状态；异常退出不向已中止事务发恢复SQL，也不遮盖原错。
            event.remove(connection, "before_cursor_execute", before)
            connection.info.pop(_STATE_KEY, None)
        if completed:
            timeout = state.previous if outermost else state.timeout(strict=False)
            if timeout != state.configured:
                connection.execute(
                    text("SELECT set_config('statement_timeout', :value, true)"),
                    {"value": f"{timeout}ms"},
                    execution_options={_CONTROL_OPTION: True},
                )
                state.configured = timeout
        else:
            # 由调用方恢复SAVEPOINT；保留父期限，失效可能被回滚的配置缓存。
            state.configured = None
