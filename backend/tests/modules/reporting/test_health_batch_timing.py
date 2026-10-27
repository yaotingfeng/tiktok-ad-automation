"""健康分批按实际耗时调整，慢成功不扩批，完整范围才发布。"""

import json
from datetime import datetime

import pytest

from app.modules.reporting import health
from app.modules.reporting.store_models import ReportSyncHealth
from tests.modules.reporting.test_health_adaptive_batches import _pool


def _timed_measure(monkeypatch, duration):
    clock = [0.0]
    original = health.measure_coverage

    def measure(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += duration[0]
        return result

    monkeypatch.setattr(health, "monotonic", lambda: clock[0])
    monkeypatch.setattr(health, "measure_coverage", measure)


def _finish(args, key, factory, seed, redis_client, duration, original_at):
    duration[0] = 0.2
    cycle = json.loads(redis_client.get(key))["cycle"]
    for _ in range(10):
        result = health.run_bc_health(**args, cycle=cycle)
        if result == "COMPLETE":
            break
        assert result == "CONTINUE"
    assert result == "COMPLETE" and redis_client.get(key) is None
    with factory() as session:
        row = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
        assert (
            row.metrics["stale_ad_accounts"]
            == row.metrics["stale_material_accounts"]
            == 128
        )
        assert row.metrics["coverage_observed_at"] == original_at
        assert row.observed_at <= datetime.fromisoformat(original_at)


@pytest.mark.parametrize("elapsed,expected", [(1.6, 32), (1.5, 64)])
def test_slow_success_shrinks_next_batch_without_discarding_measured_accounts(
    committed_sync_seed, redis_client, monkeypatch, elapsed, expected
):
    seed, factory = committed_sync_seed
    args, key = _pool(seed, factory, monkeypatch, redis_client)
    duration = [elapsed]
    _timed_measure(monkeypatch, duration)
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        cycle = json.loads(redis_client.get(key))["cycle"]
        assert health.run_bc_health(**args, cycle=cycle) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        assert progress["offset"] == 64 and progress["batch_size"] == expected
        assert progress["success_streak"] == 0
        with factory() as session:
            metrics = session.get(ReportSyncHealth, (seed.tenant_id, "bc-a")).metrics
            assert metrics["prior_complete"] == 1
            assert "missing_directory_ads" not in metrics
        _finish(args, key, factory, seed, redis_client, duration, progress["at"])
    finally:
        redis_client.delete(key)


@pytest.mark.parametrize("elapsed,expected,first_streak", [(1.2, 16, 0), (0.4, 32, 1)])
def test_only_fast_consecutive_successes_regrow_a_reduced_batch(
    committed_sync_seed, redis_client, monkeypatch, elapsed, expected, first_streak
):
    seed, factory = committed_sync_seed
    args, key = _pool(seed, factory, monkeypatch, redis_client)
    duration = [0.2]
    _timed_measure(monkeypatch, duration)
    try:
        assert health.run_bc_health(**args) == "CONTINUE"
        cycle = json.loads(redis_client.get(key))["cycle"]
        assert health.run_bc_health(**args, cycle=cycle) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        original_at = progress["at"]
        progress.update(batch_size=16, success_streak=0)
        redis_client.set(key, json.dumps(progress), ex=180)
        duration[0] = elapsed
        assert health.run_bc_health(**args, cycle=cycle) == "CONTINUE"
        assert json.loads(redis_client.get(key))["success_streak"] == first_streak
        assert health.run_bc_health(**args, cycle=cycle) == "CONTINUE"
        progress = json.loads(redis_client.get(key))
        assert progress["offset"] == 96 and progress["batch_size"] == expected
        _finish(args, key, factory, seed, redis_client, duration, original_at)
    finally:
        redis_client.delete(key)
