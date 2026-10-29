"""真实PG：维护尾部超时不能撤销本轮前项及失败轮转标记。"""

from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from app.modules.reporting import link_work, material_links
from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_link_maintenance_budget import waiting


@pytest.mark.parametrize("failure", ["57014", "55P03", "40P01", "budget"])
def test_late_probe_preserves_committed_wake_and_failure_rotation(
    committed_sync_seed, monkeypatch, failure
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    lock_key = uuid4().int % (2**63 - 1)
    with factory() as session, session.begin():
        first = waiting(session, seed, now).id
        failed = waiting(session, seed, now, 1).id
        untouched = waiting(session, seed, now, 2).id

    def probe(db, job):
        if job.id == first:
            return True
        if job.id == failed:
            # 探针内部写入也必须回滚，失败轮转不能伪造依赖变化。
            job.progress = job.progress | {"uncommitted_probe": True}
            db.flush()
            if failure == "57014":
                try:
                    db.execute(text("SELECT pg_sleep(2)"))
                finally:
                    # 真实取消后的收尾也花时间，不能依赖50ms向下取整碰巧留下余量。
                    sleep(0.075)
            elif failure == "budget":
                raise QueryBudgetExceeded()
            elif failure == "55P03":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
                )
            else:
                db.execute(
                    text(
                        "DO $$ BEGIN RAISE EXCEPTION 'test contention' USING ERRCODE = '"
                        + failure
                        + "'; END $$"
                    )
                )
        return False

    monkeypatch.setattr(material_links, "dependency_changed", probe)
    start = monotonic()
    with factory() as blocker:
        if failure == "55P03":
            # 两个真实连接竞争锁，验证PG锁超时不会毒化后续轮转写入。
            blocker.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
            )
        with factory() as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout='30ms'"))
            with query_budget(session, seconds=1):
                assert (
                    link_work.wake_link_dependencies(
                        session,
                        now=now,
                        limit=2,
                        deadline=datetime.now(UTC) + timedelta(seconds=1),
                    )
                    == 1
                )
    assert monotonic() - start < 1.3
    with factory() as session:
        good = session.get(ReportSyncJob, first)
        bad = session.get(ReportSyncJob, failed)
        assert good.due_at == now and good.error_code is None
        assert bad.due_at == now + timedelta(hours=2)
        assert bad.error_code == "material_directory_pending"
        assert bad.progress["dependency_probed_at"] == now.isoformat()
        assert "uncommitted_probe" not in bad.progress
        assert "dependency_attempts" not in bad.progress

    # 下轮先触达尚未尝试项，而非又被失败队首堵住。
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: False)
    with factory() as session, session.begin():
        link_work.wake_link_dependencies(
            session, now=now + timedelta(seconds=2), limit=1
        )
    with factory() as session:
        assert (
            session.get(ReportSyncJob, untouched).progress["dependency_probed_at"]
            == (now + timedelta(seconds=2)).isoformat()
        )


def test_next_candidate_select_timeout_keeps_previous_wake(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        first = waiting(session, seed, now).id
        second = waiting(session, seed, now, 1).id
    monkeypatch.setattr(material_links, "dependency_changed", lambda *_: True)
    with factory() as session, session.begin():
        connection = session.connection()
        with query_budget(session, seconds=1):

            def delay_second(_conn, _cursor, statement, parameters, _context, _many):
                if "FOR UPDATE" in statement and second in parameters.values():
                    statement = statement.replace(
                        "WHERE ", "WHERE pg_sleep(2) IS NULL AND ", 1
                    )
                return statement, parameters

            event.listen(connection, "before_cursor_execute", delay_second, retval=True)
            try:
                assert (
                    link_work.wake_link_dependencies(
                        session,
                        now=now,
                        deadline=datetime.now(UTC) + timedelta(seconds=1),
                    )
                    == 1
                )
            finally:
                event.remove(connection, "before_cursor_execute", delay_second)
    with factory() as session:
        assert session.get(ReportSyncJob, first).due_at == now
        pending = session.get(ReportSyncJob, second)
        assert pending.due_at > now
        assert "dependency_probed_at" not in pending.progress
