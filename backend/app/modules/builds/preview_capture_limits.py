"""请求线程冻结的硬边界；繁忙时回滚本次完整快照，不延长锁等待。"""

from contextlib import contextmanager
from time import monotonic

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.core.errors import DomainError

QUERY_TIMEOUT_MS = 5000
LOCK_TIMEOUT_MS = 1000
CAPTURE_SECONDS = 10


@contextmanager
def freeze_capture_limits(session):
    deadline = monotonic() + CAPTURE_SECONDS
    try:
        with session.begin_nested():
            connection = session.connection()
            previous = session.execute(
                text(
                    "SELECT current_setting('statement_timeout'),current_setting('lock_timeout'),(SELECT setting::int FROM pg_settings WHERE name='statement_timeout'),(SELECT setting::int FROM pg_settings WHERE name='lock_timeout')"
                )
            ).one()
            session.execute(
                text(
                    "SELECT set_config('statement_timeout',:query,true),set_config('lock_timeout',:lock,true)"
                ),
                {
                    "query": str(
                        min(QUERY_TIMEOUT_MS, previous[2] or QUERY_TIMEOUT_MS)
                    ),
                    "lock": str(min(LOCK_TIMEOUT_MS, previous[3] or LOCK_TIMEOUT_MS)),
                },
            )

            def check_deadline(*_args):
                if monotonic() >= deadline:
                    raise DomainError(
                        "preview_freeze_busy",
                        "预览冻结繁忙，请稍后重试；本次未保存部分内容",
                    )

            event.listen(connection, "before_cursor_execute", check_deadline)
            try:
                yield
                check_deadline()
            finally:
                # 先移除守卫再由 savepoint 回滚，不能阻止错误清理 SQL。
                event.remove(connection, "before_cursor_execute", check_deadline)
            session.execute(
                text(
                    "SELECT set_config('statement_timeout',:query,true),set_config('lock_timeout',:lock,true)"
                ),
                {"query": previous[0], "lock": previous[1]},
            )
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in {"57014", "55P03"}:
            raise DomainError(
                "preview_freeze_busy", "预览冻结繁忙，请稍后重试；本次未保存部分内容"
            ) from None
        raise
