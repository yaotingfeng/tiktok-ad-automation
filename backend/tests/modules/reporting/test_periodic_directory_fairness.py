"""周期定向刷新只唤醒公平派发；新一轮不能绕过已经排队的账户。"""

from datetime import UTC, datetime, timedelta

from sqlmodel import delete, select

from app.core.config import settings
from app.integrations.tiktok.contracts.ads import EntityRef
from app.jobs.models import PendingDispatch
from app.modules.ads import directory_dispatch
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from app.modules.reporting.directory_refresh import refresh_report_directory
from tests.modules.reporting.test_directory_refresh import prepared
from tests.modules.reporting.test_jobs import _route


def dispatches(session, seed):
    return session.exec(
        select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
    ).all()


def test_new_periodic_refresh_wakes_dispatch_without_overtaking_backlog(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(directory_dispatch, "TARGETED_DISPATCH_WINDOW", 1)
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        frozen_route=_route(sync_seed),
        advertiser_ids=["acc-1"],
        scope="targeted",
        refs=[EntityRef(sync_seed.tenant_id, "acc-1", "ad", "old-ad")],
        ad_types={"acc-1": {"SMART_PLUS": ("ad",)}},
    )
    old = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).one()
    old.created_at = datetime.now(UTC) - timedelta(hours=1)
    # 模拟窗口此前全满留下的持久积压，现在有一个空槽。
    session.exec(
        delete(PendingDispatch).where(PendingDispatch.tenant_id == sync_seed.tenant_id)
    )
    job = prepared(session, sync_seed)

    assert refresh_report_directory(session, job) == 3
    pending = dispatches(session, sync_seed)
    assert len(pending) == 1
    assert pending[0].task_name == "reporting.plan_due"
    assert pending[0].payload == {"dispatch_only": True}
    assert pending[0].published_at is None
    assert pending[0].available_at <= datetime.now(UTC)
    assert refresh_report_directory(session, job) == 0
    assert len(dispatches(session, sync_seed)) == 1

    assert job_tasks._dispatch_directories(session, datetime.now(UTC)) == 1
    targeted = [
        row
        for row in dispatches(session, sync_seed)
        if row.task_name == "ads.targeted_step"
    ]
    assert [row.payload["run_id"] for row in targeted] == [str(old.id)]


def test_request_wakeup_does_not_consume_completion_wakeup_key(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(session, sync_seed)
    assert refresh_report_directory(session, job) == 3
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    for run in runs:
        directory_dispatch.enqueue_directory_refill(session, run)
    wakeups = [
        row
        for row in dispatches(session, sync_seed)
        if row.task_name == "reporting.plan_due"
    ]
    assert len(wakeups) == 4
