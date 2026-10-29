"""嵌套预算不能阻止SAVEPOINT恢复，退出子预算也不能放宽父级剩余期限。"""

from datetime import UTC, datetime
from importlib import import_module

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import select

from app.core.db import engine
from app.modules.reporting import job_tasks, planner
from app.modules.reporting.planning_steps import PHASE_ORDER, _prepare, plan_step
from app.modules.reporting.query_budget import query_budget
from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob


def _timeout_ms(session):
    # 绕开SQLAlchemy预算事件，直接观察服务端实际值，避免下一条查询掩盖错误恢复。
    with session.connection().connection.driver_connection.cursor() as cursor:
        cursor.execute(
            "SELECT setting::int FROM pg_settings WHERE name='statement_timeout'"
        )
        return cursor.fetchone()[0]


def test_inner_cancel_allows_outer_savepoint_recovery(session, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        import_module("app.modules.reporting.query_budget"),
        "monotonic",
        lambda: clock[0],
    )
    with query_budget(session, seconds=3):
        with pytest.raises(DBAPIError) as caught:
            with session.begin_nested(), query_budget(session, seconds=1):
                try:
                    session.execute(text("SELECT pg_cancel_backend(pg_backend_pid())"))
                    session.execute(text("SELECT 1"))
                except DBAPIError:
                    clock[0] = 0.5
                    raise
        assert getattr(caught.value.orig, "sqlstate", None) == "57014"
        assert session.execute(text("SELECT 42")).scalar_one() == 42
        assert 0 < _timeout_ms(session) <= 3000


def test_normal_inner_exit_restores_parent_remaining_not_entry_timeout(
    session, monkeypatch
):
    module = import_module("app.modules.reporting.query_budget")
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    session.execute(text("SET LOCAL statement_timeout='12s'"))
    with query_budget(session, seconds=10):
        session.execute(text("SELECT 1"))
        with query_budget(session, seconds=4):
            session.execute(text("SELECT 1"))
            clock[0] = 3
            session.execute(text("SELECT 1"))
        assert _timeout_ms(session) == 7000
        session.execute(text("SELECT 1"))
        assert _timeout_ms(session) == 7000
        clock[0] = 4
        session.execute(text("SELECT 1"))
        assert _timeout_ms(session) == 6000
    assert _timeout_ms(session) == 12000


def test_directory_cancel_keeps_prior_commit_and_stage_retry(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        planner.ensure_bc_plans(session, now=now)
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        accounts = planner.owned_accounts(session, plan)
        state = _prepare(
            plan, accounts, now, planner._timezones(session, plan.tenant_id, accounts)
        )
        state["phase"] = PHASE_ORDER.index("directory")
        plan.screening_progress = {"planning_steps_v1": state}
        plan.active_screened_at = now
        planner.plan_hot(session, plan, active=["acc-1"], now=now)

    clock = [0.0]
    monkeypatch.setattr(
        import_module("app.modules.reporting.query_budget"),
        "monotonic",
        lambda: clock[0],
    )

    def cancelled(session, **_kwargs):
        try:
            session.execute(text("SELECT pg_cancel_backend(pg_backend_pid())"))
            session.execute(text("SELECT 1"))
        except DBAPIError:
            clock[0] = 0.5
            raise

    monkeypatch.setattr(job_tasks, "directory_unblocked_accounts", cancelled)
    assert plan_step(
        engine, redis_client, (seed.tenant_id, "bc-a"), now=now, allow_backfill=True
    )
    with factory() as session:
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.active_screened_at == now
        assert plan.next_directory_at > now
        state = plan.screening_progress["planning_steps_v1"]
        assert state.get("retry_at") is None
        assert state.get("directory_retry") is not None
        assert session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.kind == "ad_day",
            )
        ).all()


def test_parent_expiry_allows_recovery_but_still_blocks_business_sql(
    session, monkeypatch
):
    module = import_module("app.modules.reporting.query_budget")
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    with query_budget(session, seconds=1):
        with pytest.raises(module.QueryBudgetExceeded):
            with session.begin_nested(), query_budget(session, seconds=4):
                clock[0] = 2
                session.execute(text("SELECT 1"))
        with pytest.raises(module.QueryBudgetExceeded):
            session.execute(text("SELECT 2"))
    assert module._STATE_KEY not in session.connection().info
    assert session.execute(text("SELECT 3")).scalar_one() == 3


def test_existing_short_timeout_and_exception_cleanup_are_preserved(session):
    module = import_module("app.modules.reporting.query_budget")
    session.execute(text("SET LOCAL statement_timeout='75ms'"))
    with pytest.raises(RuntimeError, match="original"), session.begin_nested():
        with query_budget(session, seconds=10):
            with query_budget(session, seconds=5):
                session.execute(text("SELECT 1"))
                assert _timeout_ms(session) == 75
            assert _timeout_ms(session) == 75
            raise RuntimeError("original")
    assert module._STATE_KEY not in session.connection().info
    assert _timeout_ms(session) == 75
    assert session.execute(text("SELECT 42")).scalar_one() == 42
