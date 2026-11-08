"""复用已发布经营版本时，候选检查不重复传输和解码大JSON快照。"""

from datetime import UTC, date, datetime

from sqlalchemy import event

from app.modules.business.projection import request_rebuild
from tests.modules.business.test_projection import build, seed_days


def test_published_candidate_lookup_reuses_loaded_snapshot(session, business_case):
    c = business_case
    seed_days(session, c)
    published = build(session, c)
    statements = []
    connection = session.connection()

    def capture(_connection, _cursor, statement, _params, _context, _many):
        if "business_run.source_signature =" in statement:
            statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        reused = request_rebuild(
            session,
            context=c.context,
            bc_id=c.bc_id,
            start_date=date(2026, 10, 1),
            end_date=c.day,
            now=datetime(2026, 11, 4, 12, tzinfo=UTC),
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert reused == published and all(run.status == "COMPLETE" for run in reused)
    assert statements
    assert all(
        "business_run.inputs" not in sql.split("FROM", 1)[0] for sql in statements
    )


def test_scheduler_soft_timeout_discards_the_physical_connection(monkeypatch, session):
    from uuid import uuid4

    import pytest
    from billiard.exceptions import SoftTimeLimitExceeded
    from sqlalchemy import create_engine, text

    from app.core.db import engine
    from app.modules.business import tasks

    assert session.connection() is not None
    isolated = create_engine(engine.url, pool_size=1, max_overflow=0, pool_timeout=1)
    old_pids = []
    lock_key = uuid4().int % (2**63 - 1)

    def interrupted(db, **_kwargs):
        old_pids.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(tasks, "engine", isolated)
    monkeypatch.setattr(tasks, "schedule_due", interrupted)
    try:
        with pytest.raises(SoftTimeLimitExceeded):
            tasks.schedule_business()
        with isolated.connect() as connection:
            assert (
                connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
                != old_pids[0]
            )
            assert connection.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key}
            ).scalar_one()
    finally:
        isolated.dispose()


def test_source_snapshot_reads_value_rows_without_orm_state_hydration(
    session, business_case
):
    from sqlmodel import select

    from app.modules.business.models import BusinessScope
    from app.modules.business.source import source_snapshot
    from app.modules.reporting.store_models import ReportSyncState

    c = business_case
    seed_days(session, c)
    build(session, c)
    scope = session.exec(
        select(BusinessScope).where(BusinessScope.tenant_id == c.context.tenant_id)
    ).one()
    when = datetime(2026, 11, 4, 12, tzinfo=UTC)
    expected, signature = source_snapshot(
        session, scope, date(2026, 10, 1), c.day, when
    )
    assert len(expected["states"]) == 34
    for instance in list(session.identity_map.values()):
        if isinstance(instance, ReportSyncState):
            session.expunge(instance)
    hydrated = []

    def loaded(_session, instance):
        if isinstance(instance, ReportSyncState):
            hydrated.append(instance)

    event.listen(session, "loaded_as_persistent", loaded)
    try:
        actual, actual_signature = source_snapshot(
            session, scope, date(2026, 10, 1), c.day, when
        )
    finally:
        event.remove(session, "loaded_as_persistent", loaded)
    assert actual == expected and actual_signature == signature
    assert not hydrated, "来源签名只需字段值，不应构造全部日状态ORM对象"


def test_cleanup_candidate_omits_large_frozen_json(session, business_case):
    from app.modules.business.maintenance import cleanup_business

    c = business_case
    seed_days(session, c)
    build(session, c)
    statements = []
    connection = session.connection()

    def capture(_connection, _cursor, statement, _params, _context, _many):
        if "FROM business_run" in statement and "FOR UPDATE" in statement:
            statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        cleanup_business(session, now=datetime(2026, 11, 10, tzinfo=UTC))
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert statements
    assert all(
        "business_run.inputs" not in sql.split("FROM", 1)[0]
        and "business_run.coverage" not in sql.split("FROM", 1)[0]
        for sql in statements
    )
