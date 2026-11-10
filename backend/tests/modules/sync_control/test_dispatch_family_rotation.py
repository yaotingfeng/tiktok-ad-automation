"""慢家族耗尽真实SQL预算后，下一次唤醒仍给其它家族首轮机会。"""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.db import engine
from app.modules.reporting import job_tasks


@pytest.mark.usefixtures("committed_sync_seed", "redis_client")
@pytest.mark.parametrize("include_directories", [True, False])
def test_sql_budget_exhaustion_rotates_first_family_across_wakes(
    monkeypatch, include_directories
):
    from app.modules.sync_control import balance_demand

    monkeypatch.setattr(job_tasks.settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    called = []

    def slow_family(name):
        def dispatch(session, *_args, **_kwargs):
            called.append(name)
            # 真实PG触发当前事务的statement_timeout；不是伪造异常或平台请求。
            session.execute(text("SELECT pg_sleep(2)"))
            pytest.fail("family query must remain bounded")

        return dispatch

    monkeypatch.setattr(job_tasks, "_dispatch_report_jobs", slow_family("report"))
    monkeypatch.setattr(job_tasks, "_dispatch_directories", slow_family("directory"))
    monkeypatch.setattr(balance_demand, "dispatch_balances_due", slow_family("balance"))
    expected = (
        ["report", "directory", "balance"]
        if include_directories
        else ["report", "balance"]
    )
    for _ in expected:
        assert (
            job_tasks._dispatch_pending(
                engine,
                datetime.now(UTC),
                include_directories=include_directories,
                budget_seconds=1,
            )
            == 0
        )
    assert called == expected


def test_full_live_and_background_pools_do_not_offer_a_history_link_head(
    session, sync_seed
):
    from datetime import timedelta

    from sqlmodel import select

    from app.modules.reporting.jobs import enqueue_job
    from app.modules.sync_control.dispatcher import _pool_ready_heads
    from app.modules.sync_control.models import SyncSchedule
    from tests.modules.reporting.test_jobs import _route

    now = datetime.now(UTC)
    day = now.date() - timedelta(days=1)
    enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        kind="creative_day",
        tier="history",
        advertiser_ids=["acc-1"],
        campaign_ids=["campaign"],
        start=day,
        end=day,
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        due_at=now,
    )
    assert session.exec(select(SyncSchedule.scope_id)).all()
    assert not session.exec(
        select(SyncSchedule.scope_id).where(
            *_pool_ready_heads(live_remaining=0, background_remaining=0, now=now)
        )
    ).all()
