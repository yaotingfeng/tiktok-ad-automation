"""一次数据库往返超过软探测预算时，仍保留有界的家族发现机会。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import event, insert
from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control import worker_pool
from app.modules.sync_control.models import (
    SyncExecutionScope,
    SyncRequirement,
    SyncSchedule,
    SyncScope,
)
from tests.modules.reporting.test_jobs import _route
from tests.modules.sync_control.test_schedule_fencing import _directory


def report(session, seed, kind, now):
    return enqueue_job(
        session,
        tenant_id=seed.tenant_id,
        actor_id=seed.actor_id,
        bc_id="bc-a",
        kind=kind,
        tier="history",
        advertiser_ids=["acc-1"],
        start=now.date() - timedelta(days=2),
        end=now.date() - timedelta(days=2),
        frozen_route=_route(seed),
        due_at=now,
    )


def slow_sql_clock(session, monkeypatch):
    # 只控制探测器的软时钟；PG和Redis仍真实执行，模拟线上单次SQL>30ms。
    ticks = [0.0]
    monkeypatch.setattr(worker_pool, "monotonic", lambda: ticks[0])

    def finished(*_args):
        ticks[0] += 0.05

    connection = session.connection()
    event.listen(connection, "after_cursor_execute", finished)
    return connection, finished


def test_slow_inventory_cannot_skip_every_competing_family(
    session, sync_seed, redis_client, monkeypatch
):
    now = datetime.now(UTC)
    report(session, sync_seed, "ad_day", now)
    run = _directory(session, sync_seed)
    run.workload = "background"
    session.flush()
    now = datetime.now(UTC) + timedelta(seconds=1)
    connection, listener = slow_sql_clock(session, monkeypatch)
    seen = set()
    try:
        for _ in range(3):
            seen.update(
                worker_pool._ready_categories(
                    session,
                    redis_client,
                    category="facts",
                    now=now,
                    allow_backfill=True,
                    budget_seconds=0.03,
                    probe_limit=64,
                )
            )
    finally:
        event.remove(connection, "after_cursor_execute", listener)
    assert "directory" in seen


def test_slow_page_skips_retired_owner_prefix_before_first_quota_probe(
    session, sync_seed, redis_client, monkeypatch
):
    now = datetime.now(UTC)
    original = report(session, sync_seed, "ad_day", now)
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == original)
    ).one()
    models = (
        SyncScope,
        SyncRequirement,
        ReportSyncJob,
        SyncExecutionScope,
        SyncSchedule,
    )
    templates = (
        session.get(SyncScope, child.scope_id).model_dump(),
        session.get(SyncRequirement, child.requirement_id).model_dump(),
        session.get(ReportSyncJob, original).model_dump(),
        child.model_dump(),
        session.get(SyncSchedule, (child.scope_id, "history")).model_dump(),
    )
    batches = [[] for _ in models]
    for i in range(1, 64):
        scope, requirement, job = UUID(int=i), uuid4(), uuid4()
        rows = [dict(template) for template in templates]
        rows[0].update(id=scope, identity_digest=f"{i:064x}", owner_epoch=2)
        rows[1].update(id=requirement, scope_id=scope)
        rows[2].update(id=job, job_key=str(job))
        rows[3].update(
            id=uuid4(), scope_id=scope, requirement_id=requirement, report_job_id=job
        )
        rows[4].update(scope_id=scope)
        for batch, row in zip(batches, rows, strict=True):
            batch.append(row)
    for model, rows in zip(models, batches, strict=True):
        session.connection().execute(insert(model), rows)
    run = _directory(session, sync_seed)
    run.workload = "background"
    session.flush()
    connection, listener = slow_sql_clock(session, monkeypatch)
    try:
        ready = worker_pool._ready_categories(
            session,
            redis_client,
            category="directory",
            now=now,
            allow_backfill=True,
            budget_seconds=0.03,
            probe_limit=64,
        )
    finally:
        event.remove(connection, "after_cursor_execute", listener)
    assert "facts" in ready
