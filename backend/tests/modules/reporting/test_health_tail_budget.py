"""真实PG尾片合作执行；不足完整片的取消不能惩罚批大小或公布失败。"""

import json
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.reporting import health
from app.modules.reporting.store_models import (
    AdReportDay,
    MaterialReportDay,
    ReportSyncHealth,
)
from tests.modules.reporting.test_health_continuation import _case


def _tail_case(seed, factory, monkeypatch, redis_client):
    args = _case(seed, factory, monkeypatch) | {"redis_client": redis_client}
    monkeypatch.setattr(health, "HEALTH_CHUNK_SIZE", 64)
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        db.execute(
            text(
                "UPDATE advertiser_account SET timezone='UTC' WHERE tenant_id=:tenant"
            ),
            {"tenant": seed.tenant_id},
        )
        for model, fields in [
            (AdReportDay, {"ad_id": "tail-ad", "adgroup_id": "g", "campaign_id": "c"}),
            (
                MaterialReportDay,
                {"main_material_id": "tail-post", "main_material_type": "VIDEO"},
            ),
        ]:
            db.execute(
                insert(model),
                [
                    {
                        "tenant_id": seed.tenant_id,
                        "advertiser_id": account,
                        "stat_date": now.date(),
                        "currency": "USD",
                        "timezone": "UTC",
                        "fetched_at": now,
                        "sync_job_id": uuid4(),
                        "spend": 1,
                        **fields,
                    }
                    for account in ["acc-1", "acc-2"]
                ],
            )
        db.add(
            ReportSyncHealth(tenant_id=seed.tenant_id, bc_id="bc-a", last_repair_at=now)
        )
    key = health._health_progress_key(seed.tenant_id, "bc-a")
    assert health.run_bc_health(**args) == "CONTINUE"
    progress = json.loads(redis_client.get(key))
    return args, key, progress


def _short_entry(monkeypatch, seconds=1.7):
    advance = health._advance_health

    def short(db, plan, redis_client, *, cycle, deadline):
        return advance(
            db,
            plan,
            redis_client,
            cycle=cycle,
            deadline=min(deadline, monotonic() + seconds),
        )

    monkeypatch.setattr(health, "_advance_health", short)


def test_real_small_batches_use_the_remaining_one_second(
    committed_sync_seed, redis_client, monkeypatch
):
    seed, factory = committed_sync_seed
    args, key, progress = _tail_case(seed, factory, monkeypatch, redis_client)
    _short_entry(monkeypatch)
    try:
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "COMPLETE"
        assert redis_client.get(key) is None
        with factory() as db:
            row = db.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert (
                row.metrics["missing_directory_ads"]
                == row.metrics["unlinked_material_posts"]
                == 2
            )
            assert row.metrics["coverage_observed_at"] == progress["at"]
    finally:
        redis_client.delete(key)


@pytest.mark.parametrize("tail_batch", [64, 1])
def test_real_tail_57014_preserves_prefix_batch_and_normal_continuation(
    committed_sync_seed, redis_client, monkeypatch, caplog, tail_batch
):
    seed, factory = committed_sync_seed
    args, key, progress = _tail_case(seed, factory, monkeypatch, redis_client)
    _short_entry(monkeypatch)
    progress["stages"]["material"]["batch_size"] = tail_batch
    redis_client.set(key, json.dumps(progress), ex=180)
    original = health.measure_coverage
    canceled = []
    stages = []

    def measure(db, *args, **kwargs):
        stages.append(kwargs["stage"])
        if kwargs["stage"] == "material":
            try:
                db.execute(text("SELECT pg_sleep(2)"))
            except DBAPIError as error:
                canceled.append(getattr(error.orig, "sqlstate", None))
                raise
        return original(db, *args, **kwargs)

    monkeypatch.setattr(health, "measure_coverage", measure)
    monkeypatch.setattr(health.logger, "disabled", False)
    caplog.set_level("INFO", logger=health.__name__)
    try:
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "CONTINUE"
        assert canceled == ["57014"]
        saved = json.loads(redis_client.get(key))
        assert saved["cycle"] == progress["cycle"] and saved["at"] == progress["at"]
        assert saved["metrics"] == {"missing_directory_ads": 2}
        assert saved["stages"]["directory"]["offset"] == 2
        assert saved["stages"]["material"] == progress["stages"]["material"]
        assert saved["retry_not_before"] is None and saved["offset"] == 0
        assert saved["continuation_seq"] == progress["continuation_seq"] + 1
        assert (
            redis_client.get(health._health_batch_hint_key(seed.tenant_id, "bc-a"))
            is None
        )
        assert "outcome=tail_yield" in caplog.text
        assert "slice=tail" in caplog.text
        with factory() as db:
            row = db.get(ReportSyncHealth, (seed.tenant_id, "bc-a"))
            assert row.metrics["coverage_status"] == "PENDING"
            assert "missing_directory_ads" not in row.metrics
            tasks = db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_name == health.HEALTH_BC_TASK,
                )
            ).all()
            assert len(tasks) == 2 and tasks[-1].payload["cycle"] == progress["cycle"]

        def recovered(db, *args, **kwargs):
            stages.append(kwargs["stage"])
            return original(db, *args, **kwargs)

        monkeypatch.setattr(health, "measure_coverage", recovered)
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "COMPLETE"
        assert stages == ["directory", "material"] + ["material"] * (
            2 if tail_batch == 1 else 1
        )
    finally:
        redis_client.delete(key, health._health_batch_hint_key(seed.tenant_id, "bc-a"))


def test_full_slice_keeps_timeout_adaptation_when_sql_listener_rounds_below_three(
    committed_sync_seed, redis_client, monkeypatch, caplog
):
    seed, factory = committed_sync_seed
    args, key, progress = _tail_case(seed, factory, monkeypatch, redis_client)
    timeouts = []
    original = health.measure_coverage

    def fail(db, *args, **kwargs):
        timeouts.append(
            db.execute(text("SELECT current_setting('statement_timeout')::interval"))
            .scalar_one()
            .total_seconds()
        )
        db.execute(text("SELECT pg_sleep(3.1)"))
        return original(db, *args, **kwargs)

    monkeypatch.setattr(health, "measure_coverage", fail)
    monkeypatch.setattr(health.logger, "disabled", False)
    caplog.set_level("INFO", logger=health.__name__)
    try:
        assert health.run_bc_health(**args, cycle=progress["cycle"]) == "FAILED"
        assert len(timeouts) == 1 and 0 < timeouts[0] < 3
        saved = json.loads(redis_client.get(key))
        assert saved["stages"]["directory"]["batch_size"] == 1
        assert saved["stages"]["directory"]["offset"] == 0 and saved["metrics"] == {}
        assert "outcome=tail_yield" not in caplog.text
        assert "outcome=deferred" in caplog.text
        assert "budget=3.000s slice=full" in caplog.text
    finally:
        redis_client.delete(key, health._health_batch_hint_key(seed.tenant_id, "bc-a"))
