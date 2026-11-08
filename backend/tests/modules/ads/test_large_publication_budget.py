"""真实PG检查本地大目录预算；小目录、HTTP及原子完成语义保持。"""

from dataclasses import replace
from time import monotonic as real_monotonic

import pytest
from sqlalchemy import event, text

from app.core.config import settings
from app.modules.ads import tasks
from app.modules.ads.sync import stage_directory_page
from tests.modules.ads.test_directory_sync import _authority, _page, _run


@pytest.mark.parametrize("count,elapsed", [(1, 0), (6001, 0), (6001, 240)])
def test_publication_budget_is_size_scoped_and_uses_remaining_deadline(
    session, directory_seed, monkeypatch, count, elapsed
):
    _authority(session, directory_seed)
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(tasks, "_deliver_committed_work", lambda *_: None)
    run = _run(session, directory_seed)
    template = _page(directory_seed)
    page = replace(
        template,
        items=tuple(
            replace(
                template.items[0],
                ref=replace(
                    template.items[0].ref, remote_id=f"publication-budget-{i:05d}"
                ),
            )
            for i in range(count)
        ),
    )
    stage_directory_page(session, run_id=run.id, claim_generation=1, page=page)
    connection = session.connection()
    budgets = []
    offset = 0
    monkeypatch.setattr(
        tasks, "monotonic", lambda: real_monotonic() + offset, raising=False
    )

    def after(_connection, _cursor, statement, _params, _context, _many):
        nonlocal offset
        if "jsonb_array_length(p.items)" in statement and "sum(" in statement:
            # 只推进应用绝对预算时钟，数据库/发布和计时读取仍是真实PG。
            offset = elapsed

    def before(db, _cursor, statement, _params, _context, _many):
        if statement.lstrip().startswith(
            ("INSERT INTO ad_object ", "INSERT INTO ad_material_reference ")
        ):
            transaction, single = db.execute(
                text(
                    "SELECT extract(epoch FROM current_setting('transaction_timeout')::interval),extract(epoch FROM current_setting('statement_timeout')::interval)"
                )
            ).one()
            budgets.append(
                (
                    "material"
                    if "INSERT INTO ad_material_reference " in statement
                    else "entity",
                    float(transaction),
                    float(single),
                )
            )

    event.listen(connection, "before_cursor_execute", before)
    event.listen(connection, "after_cursor_execute", after)
    try:
        assert (
            tasks.run_directory_step(
                database_engine=connection,
                redis_client=None,
                context=directory_seed.context,
                run_id=run.id,
                claim_generation=1,
                publish_only=True,
            )
            == "READY"
        )
    finally:
        event.remove(connection, "before_cursor_execute", before)
        event.remove(connection, "after_cursor_execute", after)
    session.refresh(run)
    assert run.status == "COMPLETE" and run.published_version is not None
    assert run.claim_token is None and run.claimed_until is None
    assert {b[0] for b in budgets} == {"entity", "material"}
    for kind, transaction, single in budgets:
        if count == 1:
            assert transaction == 180 and single == (90 if kind == "material" else 30)
        else:
            assert 600 - elapsed - 30 < transaction <= 600 - elapsed
            assert single == (300 if kind == "material" else 30)


def test_large_publication_rearms_the_real_postgres_timer(monkeypatch):
    from sqlmodel import Session

    from app.core.db import engine

    monkeypatch.setattr(tasks, "_LARGE_PUBLISH_TRANSACTION_SECONDS", 1.2)
    with Session(engine) as session:
        started = real_monotonic()
        session.execute(text("SET LOCAL transaction_timeout='300ms'"))
        session.execute(text("SELECT pg_sleep(0.1)"))
        tasks._set_large_publication_timeout(session, started=started)
        # 真正跨过旧300ms期限；不能只检查GUC显示成新值。
        session.execute(text("SELECT pg_sleep(0.35)"))
        assert session.execute(text("SELECT 1")).scalar_one() == 1


def test_large_publication_real_timer_still_enforces_remaining_deadline(monkeypatch):
    from sqlalchemy.exc import DBAPIError
    from sqlmodel import Session

    from app.core.db import engine

    monkeypatch.setattr(tasks, "_LARGE_PUBLISH_TRANSACTION_SECONDS", 0.65)
    with pytest.raises(DBAPIError) as caught:
        with Session(engine) as session:
            started = real_monotonic()
            session.execute(text("SET LOCAL transaction_timeout='2s'"))
            session.execute(text("SELECT pg_sleep(0.3)"))
            tasks._set_large_publication_timeout(session, started=started)
            session.execute(text("SELECT pg_sleep(0.5)"))
    assert caught.value.connection_invalidated
    assert 0.5 < real_monotonic() - started < 1.6
