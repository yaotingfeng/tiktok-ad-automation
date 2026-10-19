"""实时/历史分类必须跨过目录依赖边界，负证据不能因时钟或普通刷新失效。"""

from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import request_link_directory
from app.modules.reporting.material_links import wait_for_dependencies
from app.modules.reporting.store_models import MaterialPostLink, ReportSyncJob
from tests.modules.reporting.test_backlog_control import request
from tests.modules.reporting.test_material_links import NOW, overview, refresh
from tests.modules.reporting.test_material_query import D, material_row, publish


def test_history_link_dependency_is_background(session, sync_seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    publish(session, sync_seed, "acc-1", [material_row("p", "5", "f.mp4")])
    session.add(
        MaterialPostLink(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            main_material_id="p",
            ad_remote_id="ad",
            main_material_type="VIDEO_SPARK_ADS",
            observed_at=datetime.now(UTC),
        )
    )
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    assert request_link_directory(session, job, now=datetime.now(UTC)) == 1
    run = session.exec(select(AdDirectoryRun)).one()
    assert run.workload == "background"
    assert not any(
        d.task_name == "ads.targeted_step"
        for d in session.exec(select(PendingDispatch)).all()
    )


def test_missing_identity_retry_backs_off_without_losing_demand(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    publish(session, sync_seed, "acc-1", [material_row("p", "5", "f.mp4")])
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    now = datetime.now(UTC)
    for minutes in (10, 20, 40, 80, 160, 320, 360, 360):
        assert wait_for_dependencies(session, job, now=now)
        assert job.due_at == now + timedelta(minutes=minutes)
        assert job.progress["waiting_ids"] == ["p"]
        now = job.due_at


def test_negative_scan_does_not_repeat_after_ten_minutes(session, sync_seed):
    from app.modules.reporting.store_models import MaterialLinkScan

    publish(session, sync_seed, "acc-1", [material_row("p", "5", "f.mp4")])
    assert refresh(session, sync_seed, overview()) == [1]
    scan = session.get(MaterialLinkScan, (sync_seed.tenant_id, "acc-1", "unscoped"))
    scan.scanned_at = NOW - timedelta(minutes=20)
    session.flush()
    assert refresh(session, sync_seed, overview()) == []


def test_refresh_timestamp_is_not_new_identity_and_reconciliation_is_bounded(
    session, sync_seed
):
    from app.modules.ads.models import AdObject
    from app.modules.reporting.store_models import MaterialLinkScan
    from tests.modules.reporting.test_material_query import use

    publish(session, sync_seed, "acc-1", [material_row("missing", "5", "v1.mp4")])
    use(session, sync_seed, adv="acc-1", ad="a1", video="v1", ad_material="material")
    assert refresh(session, sync_seed, overview())
    scan = session.get(MaterialLinkScan, (sync_seed.tenant_id, "acc-1", "unscoped"))
    scan.scanned_at = NOW - timedelta(minutes=20)
    ad = session.exec(
        select(AdObject).where(
            AdObject.tenant_id == sync_seed.tenant_id,
            AdObject.advertiser_id == "acc-1",
            AdObject.kind == "ad",
            AdObject.remote_id == "a1",
        )
    ).one()
    ad.observed_at = NOW
    session.flush()
    assert refresh(session, sync_seed, overview()) == []
    scan.scanned_at = NOW - timedelta(hours=7)
    session.flush()
    assert refresh(session, sync_seed, overview())  # 上游迟到资料仍有低频主动复核。


def test_new_identity_arriving_during_scan_is_not_negatively_cached(session, sync_seed):
    from app.modules.reporting.material_links import refresh_material_links
    from tests.modules.reporting.test_material_query import use

    publish(session, sync_seed, "acc-1", [material_row("missing", "5", "old.mp4")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="old",
        video="old",
        ad_material="old-material",
    )
    progress = {}

    def remote(_ads, _first, _last, _page):
        # 上游请求期间目录到达了新的候选，旧候选搜索不能证明新候选也无结果。
        use(
            session,
            sync_seed,
            adv="acc-1",
            ad="new",
            video="new",
            ad_material="new-material",
        )
        from app.modules.ads.models import AdMaterialReference

        ref = session.exec(
            select(AdMaterialReference).where(
                AdMaterialReference.tenant_id == sync_seed.tenant_id,
                AdMaterialReference.advertiser_id == "acc-1",
                AdMaterialReference.ad_remote_id == "new",
            )
        ).one()
        ref.name = "old.mp4"
        session.flush()
        return overview()

    args = dict(  # noqa: C408
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        read_overview=lambda _: overview(),
        read_ad_posts=remote,
        now=NOW,
        start=D,
        end=D,
        progress=progress,
    )
    assert not refresh_material_links(session, **args)
    assert refresh_material_links(session, **args)
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    assert wait_for_dependencies(session, job, now=NOW)
    assert job.due_at == NOW
    assert refresh(
        session, sync_seed, overview()
    )  # 新候选不能被上一轮未查过的负证据挡住。


def test_negative_scan_scope_and_new_ad_facts_wake_unnamed_post(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting.material_links import (
        dependency_changed,
        refresh_material_links,
    )
    from app.modules.reporting.store_models import AdReportDay

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    publish(session, sync_seed, "acc-1", [material_row("unnamed", "5")])
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    calls = []

    def run(ids):
        progress = {}
        for _ in range(5):
            if refresh_material_links(
                session,
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                start=D,
                end=D,
                overview_ad_ids=ids,
                progress=progress,
                now=NOW,
                read_overview=lambda p: calls.append((ids, p)) or overview(),
                read_ad_posts=lambda *args: overview(),
            ):
                return
        raise AssertionError("scan not complete")

    run(("a",))
    assert len(calls) == 1
    run(("a",))
    assert len(calls) == 1
    assert wait_for_dependencies(session, job, now=NOW)
    assert not dependency_changed(session, job)
    session.add(
        AdReportDay(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            ad_id="b",
            stat_date=D,
            campaign_id="c",
            adgroup_id="g",
            currency="USD",
            timezone="UTC",
            fetched_at=NOW,
            sync_job_id=job.id,
            campaign_automation_type="UPGRADED_SMART_PLUS",
            observed_at=NOW,
        )
    )
    session.flush()
    assert dependency_changed(session, job)
    run(("a", "b"))
    assert len(calls) == 2
    # 无新事实时仅查询范围变更也必须使旧负证据失效。
    run(("a", "b", "c"))
    assert len(calls) == 3


def test_candidate_date_evidence_changes_negative_cache_and_dependency_signature(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting.material_links import (
        _scan_signature,
        dependency_changed,
        directory_signature,
    )
    from app.modules.reporting.store_models import AdReportDay, MaterialReportDay

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    publish(session, sync_seed, "acc-1", [material_row("p", "5")])
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    first = D - timedelta(days=1)
    job.start_date = first

    def add_ad(day):
        session.add(
            AdReportDay(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                ad_id="owner",
                stat_date=day,
                campaign_id="c",
                adgroup_id="g",
                currency="USD",
                timezone="UTC",
                fetched_at=NOW,
                sync_job_id=job.id,
                campaign_automation_type="UPGRADED_SMART_PLUS",
            )
        )
        session.flush()

    def signature():
        return _scan_signature(session, sync_seed.tenant_id, "acc-1", ["p"], first, D)

    add_ad(first)
    assert wait_for_dependencies(session, job, now=NOW)
    before = signature()
    assert not dependency_changed(session, job)
    # 广告集合没变，但新增了帖子所在日期的广告事实；候选范围因此改变。
    add_ad(D)
    assert signature() != before
    assert dependency_changed(session, job)
    before, before_dependency = signature(), directory_signature(session, job)
    post = session.exec(
        select(MaterialReportDay).where(MaterialReportDay.main_material_id == "p")
    ).one()
    values = post.model_dump() | {"stat_date": first}
    session.add(MaterialReportDay(**values))
    session.flush()
    # 同一帖子新增日期也会扩展搜索窗口，必须重新核查负证据。
    assert signature() != before
    assert directory_signature(session, job) != before_dependency


def test_dependency_backoff_cannot_skip_six_hour_scan(session, sync_seed, monkeypatch):
    from app.modules.reporting.material_links import refresh_material_links

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    publish(session, sync_seed, "acc-1", [material_row("missing", "5")])
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    progress = {}
    calls = []

    def scan(now):
        for _ in range(5):
            if refresh_material_links(
                session,
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                start=D,
                end=D,
                now=now,
                progress=progress,
                read_overview=lambda p: calls.append(p) or overview(),
                read_ad_posts=lambda *args: overview(),
            ):
                return
        raise AssertionError("scan not complete")

    scan(NOW)
    now = NOW
    for minutes in (10, 30, 70, 150, 310, 360):
        assert wait_for_dependencies(session, job, now=now)
        assert job.due_at == NOW + timedelta(minutes=minutes)
        now = job.due_at
    progress.clear()
    scan(now)
    assert len(calls) == 2


def test_live_and_history_negative_evidence_do_not_evict_each_other(session, sync_seed):
    from app.modules.reporting.material_links import refresh_material_links
    from app.modules.reporting.store_models import MaterialLinkScan

    publish(session, sync_seed, "acc-1", [material_row("missing", "5")])
    calls = []

    def run(start, end, ids):
        progress = {}
        for _ in range(5):
            if refresh_material_links(
                session,
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                start=start,
                end=end,
                overview_ad_ids=ids,
                progress=progress,
                now=NOW,
                read_overview=lambda p: calls.append((start, end, p)) or overview(),
                read_ad_posts=lambda *args: overview(),
            ):
                return
        raise AssertionError("scan not complete")

    run(D, D, ("current",))
    run(D - timedelta(days=30), D, ("history", "current"))
    assert len(calls) == 2
    run(D, D, ("current",))
    run(D - timedelta(days=30), D, ("history", "current"))
    assert len(calls) == 2
    assert len(session.exec(select(MaterialLinkScan)).all()) == 2
