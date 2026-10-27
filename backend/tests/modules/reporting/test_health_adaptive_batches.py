"""超预算只缩小失败批，不丢进度；同断点重试必须生成新的可投递消息。"""

import json
from datetime import UTC, datetime, timedelta
from time import monotonic

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.reporting import health
from app.modules.reporting.store_models import AdSyncBc, ReportSyncHealth
from tests.modules.reporting.test_health_continuation import _case


def _pool(seed, factory, monkeypatch, redis_client):
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    monkeypatch.setattr(health, "HEALTH_CHUNK_SIZE", 64)
    with factory() as session, session.begin():
        session.execute(
            text("""
            INSERT INTO advertiser_account(tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
            SELECT :tenant,'adaptive-'||i,'','USD','UTC','ENABLE',false FROM generate_series(1,126) i
        """),
            {"tenant": seed.tenant_id},
        )
        session.execute(
            text("""
            INSERT INTO ad_sync_account(tenant_id,advertiser_id,owner_bc_id)
            SELECT :tenant,'adaptive-'||i,'bc-a' FROM generate_series(1,126) i
        """),
            {"tenant": seed.tenant_id},
        )
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        plan.last_active_advertiser_ids = health.owned_accounts(session, plan)
        session.add(
            ReportSyncHealth(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                metrics={"prior_complete": 1},
                last_repair_at=datetime.now(UTC),
            )
        )
    return args, health._health_progress_key(seed.tenant_id, "bc-a")


def _timeout(session, *_args, **_kwargs):
    session.execute(text("SET LOCAL statement_timeout='50ms'"))
    session.execute(text("SELECT pg_sleep(0.1)"))


def test_same_offset_timeouts_shrink_and_publish_distinct_immediate_retries(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args, key = _pool(seed, factory, monkeypatch, redis_client)
    original = health.measure_health
    monkeypatch.setattr(health, "measure_health", _timeout)
    try:
        cycle = None
        original_at = None
        for expected_batch in [32, 16]:
            assert health.run_bc_health(**args, cycle=cycle) == "FAILED"
            progress = json.loads(redis_client.get(key))
            assert progress["offset"] == 0 and progress["metrics"] == {}
            assert progress["batch_size"] == expected_batch
            original_at = original_at or progress["at"]
            assert progress["at"] == original_at
            cycle = progress["cycle"]
            with factory() as session, session.begin():
                row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
                assert row.metrics == {"prior_complete": 1}
                assert row.issues == ["MONITOR_QUERY_FAILED"]
                pending = session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == seed.tenant_id,
                        PendingDispatch.task_name == health.HEALTH_BC_TASK,
                        PendingDispatch.published_at.is_(None),
                    )
                ).all()
                assert len(pending) == 1 and pending[0].available_at <= datetime.now(
                    UTC
                )
                pending[0].published_at = datetime.now(UTC)
        with factory() as session:
            retries = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_name == health.HEALTH_BC_TASK,
                )
            ).all()
            assert len({row.task_key for row in retries}) == 2
        monkeypatch.setattr(health, "measure_health", original)
        sizes = []
        for _ in range(10):
            result = health.run_bc_health(**args, cycle=cycle)
            if result == "COMPLETE":
                break
            assert result == "CONTINUE"
            progress = json.loads(redis_client.get(key))
            sizes.append(progress["batch_size"])
        assert result == "COMPLETE" and 32 in sizes
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert (
                row.metrics["stale_ad_accounts"]
                == row.metrics["stale_material_accounts"]
                == 128
            )
            assert row.observed_at == datetime.fromisoformat(original_at)
    finally:
        redis_client.delete(key)


def test_single_account_timeout_cools_down_without_skipping_or_retry_storm(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    original = health.measure_health
    calls = []

    def fail(session, *args, **kwargs):
        calls.append(True)
        return _timeout(session, *args, **kwargs)

    monkeypatch.setattr(health, "measure_health", fail)
    try:
        assert health.run_bc_health(**args) == "FAILED"
        progress = json.loads(redis_client.get(key))
        assert progress["offset"] == 0 and progress["batch_size"] == 1
        assert datetime.fromisoformat(progress["retry_not_before"]) > datetime.now(
            UTC
        ) + timedelta(seconds=50)
        for cycle in [progress["cycle"], None, progress["cycle"]]:
            assert health.run_bc_health(**args, cycle=cycle) == "WAIT"
        assert len(calls) == 1
        with factory() as session:
            assert not session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_name == health.HEALTH_BC_TASK,
                )
            ).all()
        progress["retry_not_before"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        redis_client.set(key, json.dumps(progress), ex=180)
        monkeypatch.setattr(health, "measure_health", original)
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "CONTINUE"
        assert json.loads(redis_client.get(key))["offset"] == 1
    finally:
        redis_client.delete(key)


def test_timeout_restores_savepoint_and_original_statement_timeout(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    _, key = _pool(seed, factory, monkeypatch, redis_client)
    monkeypatch.setattr(health, "measure_health", _timeout)
    try:
        with factory() as session:
            session.execute(text("SET LOCAL statement_timeout='750ms'"))
            plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            with pytest.raises(health._HealthBatchTimeout):
                health._advance_health(
                    session, plan, redis_client, cycle=None, deadline=monotonic() + 10
                )
            assert session.execute(text("SELECT 1")).scalar_one() == 1
            assert (
                session.execute(text("SHOW statement_timeout")).scalar_one() == "750ms"
            )
    finally:
        redis_client.delete(key)


def test_v1_progress_defaults_preserve_already_measured_scope_and_timestamp(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        for name in [
            "batch_size",
            "success_streak",
            "continuation_seq",
            "retry_not_before",
        ]:
            progress.pop(name)
        redis_client.set(key, json.dumps(progress), ex=180)
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "COMPLETE"
        with factory() as session:
            row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.observed_at == datetime.fromisoformat(progress["at"])
            assert row.metrics["stale_ad_accounts"] == 1
            assert row.metrics["stale_material_accounts"] == 1
    finally:
        redis_client.delete(key)


def test_scope_change_during_failed_batch_does_not_enqueue_old_scope_retry(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args, key = _pool(seed, factory, monkeypatch, redis_client)
    original = health.measure_health

    def change_scope_and_fail(session, *args, **kwargs):
        with factory() as writer, writer.begin():
            plan = writer.get(AdSyncBc, (seed.tenant_id, "bc-a"))
            plan.frozen_route = plan.frozen_route | {"binding_revision": 99}
        return _timeout(session, *args, **kwargs)

    monkeypatch.setattr(health, "measure_health", change_scope_and_fail)
    try:
        assert health.run_bc_health(**args) == "FAILED"
        previous = json.loads(redis_client.get(key))
        with factory() as session:
            assert not session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_name == health.HEALTH_BC_TASK,
                )
            ).all()
        monkeypatch.setattr(health, "measure_health", original)
        assert health.run_bc_health(**args) == "CONTINUE"
        current = json.loads(redis_client.get(key))
        assert current["cycle"] != previous["cycle"]
        assert current["batch_size"] == 64 and current["offset"] == 64
        assert health.run_bc_health(**args, cycle=previous["cycle"]) == "IGNORED"
    finally:
        redis_client.delete(key)
