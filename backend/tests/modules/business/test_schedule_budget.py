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
