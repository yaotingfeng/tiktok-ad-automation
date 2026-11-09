"""报表读预算/JIT优化只作用当前事务，不能泄漏到连接池的后续业务。"""

from sqlalchemy import text

from app.core.db import engine
from app.modules.reporting.query_models import snapshot_transaction


def test_report_settings_are_local_to_snapshot_transaction():
    query = text("""SELECT current_setting('jit'),current_setting('statement_timeout'),
        current_setting('idle_in_transaction_session_timeout')""")
    with engine.connect() as connection:
        original = connection.execute(query).one()
    with snapshot_transaction(engine) as session:
        assert session.connection().execute(query).one() == ("off", "30s", "30s")
    with engine.connect() as connection:
        assert connection.execute(query).one() == original


def test_snapshot_join_preference_is_restored_after_success(report_case):
    from app.modules.reporting.queries import _build_snapshot
    from tests.modules.reporting.test_queries import filters_for

    case = report_case
    case.seed_campaign("Name", "report-account", 2, 3)
    for original in ("on", "off"):
        case.session.connection().execute(
            text("SELECT set_config('enable_nestloop',:value,true)"),
            {"value": original},
        )
        _build_snapshot(
            case.session, context=case.context, bc_id=case.bc_id, filters=filters_for()
        )
        assert (
            case.session.connection()
            .exec_driver_sql("SHOW enable_nestloop")
            .scalar_one()
            == original
        )


def test_snapshot_join_preference_is_restored_after_non_database_error(report_case):
    import pytest
    from sqlalchemy import event

    from app.modules.reporting.queries import _build_snapshot
    from tests.modules.reporting.test_queries import filters_for

    case = report_case
    case.seed_campaign("Name", "report-account", 2, 3)
    connection = case.session.connection()
    connection.exec_driver_sql("SET LOCAL enable_nestloop = on")

    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        if "INSERT INTO query_snapshot_row" in statement:
            raise RuntimeError("injected snapshot failure")

    event.listen(connection, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected snapshot failure"):
            _build_snapshot(
                case.session,
                context=case.context,
                bc_id=case.bc_id,
                filters=filters_for(),
            )
    finally:
        event.remove(connection, "before_cursor_execute", fail)
    assert connection.exec_driver_sql("SHOW enable_nestloop").scalar_one() == "on"
    assert (
        connection.execute(text("SELECT count(*) FROM query_snapshot_row")).scalar_one()
        == 0
    )


def test_database_error_rolls_back_savepoint_with_active_budget():
    from time import monotonic

    import pytest
    from sqlalchemy.exc import DBAPIError

    with snapshot_transaction(engine, deadline=monotonic() + 30) as session:
        connection = session.connection()
        connection.exec_driver_sql("SET LOCAL enable_nestloop = on")
        with pytest.raises(DBAPIError):
            with session.begin_nested():
                connection = session.connection()
                connection.exec_driver_sql("SET LOCAL enable_nestloop = off")
                connection.exec_driver_sql("SELECT 1/0")
        assert connection.exec_driver_sql("SHOW enable_nestloop").scalar_one() == "on"


def test_cancellation_does_not_block_savepoint_cleanup(session):
    import pytest
    from fastapi import HTTPException

    from app.modules.reporting.query_reuse import QueryCancellation

    cancellation = QueryCancellation()
    connection = session.connection()
    connection.exec_driver_sql("SET LOCAL enable_nestloop = on")
    with cancellation.private_query(session):
        with pytest.raises(HTTPException) as error:
            with session.begin_nested():
                connection = session.connection()
                connection.exec_driver_sql("SET LOCAL enable_nestloop = off")
                cancellation.event.set()
                connection.exec_driver_sql("SELECT 1")
        assert error.value.status_code == 499
    assert connection.exec_driver_sql("SHOW enable_nestloop").scalar_one() == "on"
