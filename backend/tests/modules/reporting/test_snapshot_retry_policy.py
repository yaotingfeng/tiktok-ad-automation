"""今日素材源集合变化短退避；其他失败策略及完整快照预算保持不变。"""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.errors import DomainError
from app.modules.reporting import jobs
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.creative_job_helpers import enqueue

NOW = datetime(2026, 10, 27, 18, tzinfo=UTC)
LOCAL_TODAY = date(2026, 10, 28)  # fixture账户为Asia/Hong_Kong，不能用UTC日期替代。


@pytest.fixture
def retry_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(jobs, "datetime", Clock)


@pytest.mark.parametrize(
    "kind,priority,day,error,delays",
    [
        (kind, priority, day, error, [60, 120, 240, 480, 960])
        for kind in ("creative_day", "ad_day")
        for priority in (1, 2, 3)
        for day in (LOCAL_TODAY, LOCAL_TODAY - timedelta(days=1))
        for error in ("report_snapshot_changed", "tiktok_response_error")
    ],
)
@pytest.mark.usefixtures("retry_clock")
def test_snapshot_retry_delay_uses_actual_local_day_only(
    committed_sync_seed, kind, priority, day, error, delays
):
    seed, factory = committed_sync_seed
    original = enqueue(factory, seed, start=day, end=day)
    with factory() as session:
        job = session.get(ReportSyncJob, original.id)
        job.kind, job.priority = kind, priority
        session.flush()
        for attempt, seconds in enumerate(delays, 1):
            assert (
                jobs._handle_error(session, job, DomainError(error, "synthetic"))
                == "WAITING"
            )
            assert job.attempts == attempt
            assert job.due_at == NOW + timedelta(seconds=seconds)
        assert (
            jobs._handle_error(session, job, DomainError(error, "synthetic"))
            == "FAILED"
        )
        assert job.attempts == 5 and job.status == "FAILED"
        session.commit()


@pytest.mark.usefixtures("retry_clock")
def test_exhausted_today_snapshot_allows_new_periodic_job(committed_sync_seed):
    seed, factory = committed_sync_seed
    original = enqueue(factory, seed, start=LOCAL_TODAY, end=LOCAL_TODAY)
    from sqlmodel import select

    from app.modules.sync_control.models import (
        SyncExecutionScope,
        SyncRequirement,
        SyncSchedule,
    )
    from tests.modules.reporting.creative_job_helpers import claim

    class ChangingReader:
        def creative_day(self, **_kwargs):
            raise DomainError("report_snapshot_changed", "synthetic")

    current = original
    for attempt in range(6):
        assert jobs.execute_job(
            factory,
            job_id=current.id,
            claim_token=current.claim_token,
            reader=ChangingReader(),
        ) == ("FAILED" if attempt == 5 else "WAITING")
        if attempt < 5:
            current = claim(factory, original.id)
    replacement = enqueue(factory, seed, start=LOCAL_TODAY, end=LOCAL_TODAY)
    assert replacement.id != original.id and replacement.attempts == 0
    with factory() as session:
        failed = session.get(ReportSyncJob, original.id)
        assert failed.status == "FAILED" and failed.attempts == 5
        assert failed.claim_token is None and failed.claimed_until is None
        old_child = session.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.report_job_id == original.id
            )
        ).one()
        assert old_child.status == "SUPERSEDED"
        requirement = session.get(SyncRequirement, old_child.requirement_id)
        assert requirement.desired_revision > old_child.frozen_revision
        head = session.exec(
            select(SyncSchedule).where(SyncSchedule.scope_id == old_child.scope_id)
        ).one()
        assert head.active and head.lease_execution_id == replacement.id
