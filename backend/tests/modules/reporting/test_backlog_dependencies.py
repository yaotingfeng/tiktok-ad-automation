"""生产尾部缺口：缺广告本体也必须补读，历史请求不能漏掉已删除对象。"""

from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.core.config import settings
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import (
    refresh_report_directory,
)
from tests.modules.reporting.test_directory_refresh import prepared


def test_general_directory_refresh_starts_before_freshness_deadline(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting import planner
    from app.modules.reporting.health import measure_health
    from app.modules.reporting.store_models import AdSyncBc

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    job = prepared(session, sync_seed)
    for kind, remote in [("ad", "a0"), ("adgroup", "g0"), ("campaign", "c0")]:
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2",
                kind=kind,
                remote_id=remote,
                ad_type="SMART_PLUS",
                published_version=1,
                observed_at=now - timedelta(minutes=26),
            )
        )
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    session.flush()
    assert measure_health(session, plan, now=now)[0]["missing_directory_ads"] == 0
    assert refresh_report_directory(session, job, now=now) == 1
    assert refresh_report_directory(session, job, now=now) == 0
    # 规划提前量与完成余量分开：等待中的37.5分钟边界健康，超过仍须告警。
    assert (
        measure_health(session, plan, now=now + timedelta(minutes=11.5))[0][
            "missing_directory_ads"
        ]
        == 0
    )
    assert (
        measure_health(session, plan, now=now + timedelta(minutes=12))[0][
            "missing_directory_ads"
        ]
        == 1
    )


def test_concurrent_missing_parent_publishers_share_one_repair(
    committed_sync_seed, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import uuid4

    from app.modules.ads.sync import _queue_missing_parents
    from tests.modules.reporting.test_jobs import _route

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    barrier = Barrier(2)

    def publish_parent_repair():
        with factory() as session:
            source = AdDirectoryRun(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                actor_id=seed.actor_id,
                advertiser_id="acc-1",
                connection_id=seed.connection.id,
                frozen_route=_route(seed),
                partition_key=uuid4().hex * 2,
                query={"include_deleted": True},
                kind="ad",
                ad_type="SMART_PLUS",
            )
            child = AdObject(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                kind="ad",
                remote_id=uuid4().hex,
                parent_kind="adgroup",
                parent_remote_id="shared-missing-parent",
                ad_type="SMART_PLUS",
                observed_at=datetime.now(UTC),
                published_version=1,
            )
            barrier.wait(timeout=5)
            _queue_missing_parents(session, run=source, objects=[child])
            session.commit()

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(publish_parent_repair) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    with factory() as session:
        runs = session.exec(select(AdDirectoryRun)).all()
        assert len(runs) == 1
        assert runs[0].kind == "adgroup"
        assert runs[0].query["ids"] == ["shared-missing-parent"]
