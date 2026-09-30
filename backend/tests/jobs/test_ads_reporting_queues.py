from pathlib import Path

from app.core.config import settings
from app.jobs.celery_app import celery_app
from app.jobs.queued_dispatches import _QUEUES
from app.jobs.tasks import dispatch_queue


def test_ads_reporting_tasks_have_dedicated_queues_and_standard_beat_gate():
    celery_app.loader.import_default_modules()
    assert dispatch_queue("ads.sync_step") == "ads-directory"
    assert dispatch_queue("reporting.sync_step") == "ads-reporting"
    assert celery_app.conf.task_routes["reporting.scan_due"]["queue"] == "control"
    assert "scan-ads-reporting-due" in celery_app.conf.beat_schedule
    assert "ads-directory" in _QUEUES
    assert "ads-reporting" in _QUEUES

    previous = settings.ADS_SYNC_ENABLED
    settings.ADS_SYNC_ENABLED = False
    try:
        from app.modules.reporting.tasks import scan_due_runs

        assert scan_due_runs(database_engine=None) == 0
    finally:
        settings.ADS_SYNC_ENABLED = previous


def test_worker_templates_keep_one_slot_and_shared_switch_injection():
    root = Path(__file__).resolve().parents[3]
    compose = (root / "compose.yml").read_text()
    production = (root / "compose.production.yml").read_text()
    directory_service = (root / "deploy/staging-ads-directory.service").read_text()
    reporting_service = (root / "deploy/staging-ads-reporting.service").read_text()
    assert "ADS_SYNC_ENABLED" in compose
    assert "-Q, ads-directory" in compose
    assert "-Q, ads-reporting" in compose
    assert "-Q, ads-directory" in production
    assert "-Q, ads-reporting" in production
    assert "--concurrency" in compose
    assert "--concurrency" in production
    assert "--concurrency 1" in directory_service
    assert "--concurrency 1" in reporting_service
