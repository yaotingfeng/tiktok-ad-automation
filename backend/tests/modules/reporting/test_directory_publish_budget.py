"""本地大目录发布有独立总预算，读取/实时回读和数据库单语句边界保持。"""

from datetime import UTC, datetime

from sqlalchemy import text

from app.core.config import settings
from app.core.db import engine
from app.modules.ads import tasks
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from tests.modules.reporting.test_directory_publish_recovery import _terminal


def test_only_local_publish_task_has_extended_deadline():
    assert (tasks.publish_step.soft_time_limit, tasks.publish_step.time_limit) == (
        195,
        210,
    )
    for task in (tasks.sync_step, tasks.targeted_step):
        assert (task.soft_time_limit, task.time_limit) == (55, 60)


def test_publish_session_budget_preserves_statement_lock_and_lease_bounds():
    for publish_only, expected in [(False, "45s"), (True, "3min")]:
        with tasks._directory_session(engine, publish_only=publish_only) as session:
            values = {
                key: session.connection().execute(text(f"SHOW {key}")).scalar_one()
                for key in (
                    "transaction_timeout",
                    "statement_timeout",
                    "lock_timeout",
                    "idle_in_transaction_session_timeout",
                )
            }
        assert values == {
            "transaction_timeout": expected,
            "statement_timeout": "30s",
            "lock_timeout": "20s",
            "idle_in_transaction_session_timeout": "10s",
        }
    # 本地发布租约独立于远端读取；事务、软限都在专用租约内。
    assert 180 < tasks.publish_step.soft_time_limit < tasks._PUBLISH_LEASE_SECONDS
    assert tasks._LEASE_SECONDS == 90


def test_atomic_publish_can_finish_after_read_budget_without_reopening_provider(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    run_id = _terminal(factory, seed, datetime.now(UTC))
    original = tasks.publish_directory
    monkeypatch.setattr(tasks, "_TRANSACTION_TIMEOUT", "150ms")
    monkeypatch.setattr(tasks, "_PUBLISH_TRANSACTION_TIMEOUT", "800ms", raising=False)

    def slow(session, **kwargs):
        row = session.get(AdDirectoryRun, run_id)
        assert (row.claimed_until - datetime.now(UTC)).total_seconds() > 209
        # 原消息已超过30分钟；发布事务行锁仍阻止扫描器抢走当前运行。
        with factory() as observer, observer.begin():
            assert job_tasks._dispatch_directories(observer, datetime.now(UTC)) == 0
        session.connection().execute(text("SELECT pg_sleep(0.25)"))
        return original(session, **kwargs)

    def forbidden_provider(**_kwargs):
        raise AssertionError("本地发布不能重开远端请求")

    monkeypatch.setattr(tasks, "publish_directory", slow)
    monkeypatch.setattr(tasks, "open_tiktok_gateway", forbidden_provider)
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=None,
            context=seed.context,
            run_id=run_id,
            claim_generation=1,
            publish_only=True,
        )
        == "READY"
    )
    with factory() as session:
        row = session.get(AdDirectoryRun, run_id)
        assert row.status == "COMPLETE" and row.published_version is not None
        assert row.claim_generation == 1 and row.claimed_until is None
