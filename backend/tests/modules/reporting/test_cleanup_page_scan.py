"""分页清理直接扫描有页记录，不被大量无页父任务拖慢。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting import job_tasks
from app.modules.reporting.store_models import ReportSyncPage
from tests.modules.reporting.test_cleanup_batches import _job
from tests.modules.reporting.test_jobs import _route


def test_report_cleanup_reaches_pages_without_scanning_empty_parents(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(job_tasks, "CLEANUP_SCAN_ROWS", 2)
    for identifier in range(1, 5):
        _job(session, sync_seed, identifier=identifier, expired=False)
    expired = _job(session, sync_seed, identifier=10, pages=1)
    active = _job(session, sync_seed, identifier=11, expired=False, pages=1)
    result = job_tasks.cleanup_store(
        session, now=datetime.now(UTC), categories=("material_pages",), cursors={}
    )
    assert result["material_pages"] == 1
    assert session.get(ReportSyncPage, (expired, 1)) is None
    assert session.get(ReportSyncPage, (active, 1)) is not None


def test_directory_cleanup_reaches_pages_without_scanning_empty_runs(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(job_tasks, "CLEANUP_SCAN_ROWS", 2)
    now = datetime.now(UTC)
    for identifier in range(1, 7):
        run = AdDirectoryRun(
            id=UUID(int=identifier),
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            bc_id="bc-a",
            actor_id=sync_seed.actor_id,
            connection_id=sync_seed.connection.id,
            frozen_route=_route(sync_seed),
            partition_key=f"{identifier:x}" * 64,
            query={},
            status="COMPLETE" if identifier == 5 else "RUNNING",
            published_version=1 if identifier == 5 else None,
            completed_at=now - timedelta(hours=2) if identifier == 5 else None,
            created_at=now - timedelta(hours=3),
            kind="ad",
            ad_type="REGULAR",
        )
        session.add(run)
        session.flush()
        if identifier >= 5:
            session.add(
                AdDirectoryPage(
                    run_id=run.id,
                    page=1,
                    tenant_id=sync_seed.tenant_id,
                    advertiser_id="acc-1",
                    claim_generation=1,
                    complete=True,
                    items=[],
                    materials=[],
                )
            )
    session.flush()
    result = job_tasks.cleanup_store(
        session, now=now, categories=("directory_pages",), cursors={}
    )
    assert result["directory_pages"] == 1
    assert session.get(AdDirectoryPage, (UUID(int=5), 1)) is None
    assert session.get(AdDirectoryPage, (UUID(int=6), 1)) is not None


def test_page_cursor_passes_retained_pages_and_resumes_after_serialization(
    session, sync_seed, monkeypatch
):
    import json

    monkeypatch.setattr(job_tasks, "CLEANUP_SCAN_ROWS", 2)
    active = _job(session, sync_seed, identifier=1, expired=False, pages=3)
    expired = _job(session, sync_seed, identifier=2, pages=2)
    cursors = {}
    now = datetime.now(UTC)
    assert (
        job_tasks.cleanup_store(
            session, now=now, categories=("material_pages",), cursors=cursors
        )["material_pages"]
        == 0
    )
    cursors = json.loads(json.dumps(cursors))
    assert (
        job_tasks.cleanup_store(
            session, now=now, categories=("material_pages",), cursors=cursors
        )["material_pages"]
        == 2
    )
    assert session.get(ReportSyncPage, (active, 3)) is not None
    assert session.get(ReportSyncPage, (expired, 2)) is None
