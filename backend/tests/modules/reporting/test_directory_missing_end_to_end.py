"""回归：新缺口不能因同租户旧状态刷新而完全失去首轮执行机会。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from app.modules.reporting.directory_refresh import refresh_report_directory
from tests.modules.reporting.test_directory_refresh import prepared
from tests.modules.reporting.test_directory_service_share import pending, sent


@pytest.mark.parametrize("old_service", ["identity", "status"])
def test_missing_demand_gets_service_with_old_backlog(
    session, sync_seed, monkeypatch, old_service
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    old_ids = set()
    for _ in range(40):
        run = pending(session, sync_seed, old_service, now, old=True)
        old_ids.add(run.id)
        session.add(
            AdObject(
                tenant_id=run.tenant_id,
                advertiser_id=run.advertiser_id,
                kind=run.kind,
                remote_id=run.query["ids"][0],
                ad_type=run.ad_type,
                observed_at=now - timedelta(hours=2),
                published_version=1,
            )
        )
    job = prepared(session, sync_seed)
    assert refresh_report_directory(session, job) == 3
    new_ids = {r.id for r in session.exec(select(AdDirectoryRun)).all()} - old_ids
    assert len(new_ids) == 3
    assert job_tasks._dispatch_directories(session, datetime.now(UTC)) == 32
    delivered_ids = {r.id for r in sent(session)}
    assert delivered_ids & new_ids, "新缺口已生成3个请求，但32个派发槽全部被旧请求占用"
