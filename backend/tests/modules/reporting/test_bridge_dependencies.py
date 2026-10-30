"""已定位广告不等于已得到帖子到 VID 的精确桥接。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import request_link_directory
from app.modules.reporting.material_links import (
    refresh_material_links,
    wait_for_dependencies,
)
from app.modules.reporting.store_models import MaterialPostLink, ReportSyncJob
from tests.modules.reporting.test_backlog_control import request
from tests.modules.reporting.test_material_links import overview
from tests.modules.reporting.test_material_query import D, material_row, publish, use


def seed_bridge(session, seed, *, complete=True, spark=True):
    row = material_row("post", "5", "file.mp4")
    kind = "VIDEO_SPARK_ADS" if spark else "VIDEO_NON_SPARK_ADS"
    row["dimensions"]["main_material_type"] = kind
    publish(session, seed, "acc-1", [row])
    use(session, seed, adv="acc-1", ad="ad", video="vid", ad_material="inner")
    usage = session.exec(
        select(AdMaterialReference).where(
            AdMaterialReference.tenant_id == seed.tenant_id,
            AdMaterialReference.advertiser_id == "acc-1",
            AdMaterialReference.ad_remote_id == "ad",
        )
    ).one()
    usage.complete = complete
    link = MaterialPostLink(
        tenant_id=seed.tenant_id,
        advertiser_id="acc-1",
        main_material_id="post",
        ad_remote_id="ad",
        main_material_type=kind,
        observed_at=datetime.now(UTC),
    )
    session.add(link)
    job = session.get(ReportSyncJob, request(session, seed, D))
    session.flush()
    return job, link


def test_known_owner_reads_late_bridge_across_overview_pages(session, sync_seed):
    _, link = seed_bridge(session, sync_seed)
    progress, calls = {}, []

    def read(page):
        calls.append(page)
        post = "post" if page == 2 else "other-post"
        rows = overview(("ad", post, "inner", "file.mp4")).rows
        return RawReportPage(rows, page, 2, 2)

    def no_search(*_args):
        raise AssertionError("已有归属不应再次定位广告")

    for expected in (False, True):
        assert (
            refresh_material_links(
                session,
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                read_overview=read,
                read_ad_posts=no_search,
                now=datetime.now(UTC),
                start=D,
                end=D,
                progress=progress,
            )
            is expected
        )
        session.flush()
    session.refresh(link)
    assert calls == [1, 2]
    assert (link.ad_material_id, link.platform_material_id) == ("inner", "vid")


def test_missing_bridge_waits_for_identity_without_repeating_complete_directory(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, link = seed_bridge(session, sync_seed)
    now = datetime.now(UTC)
    for minutes in (10, 20, 40):
        assert wait_for_dependencies(session, job, now=now)
        assert job.error_code == "material_identity_unavailable"
        assert job.due_at == now + timedelta(minutes=minutes)
        assert job.progress["waiting_ids"] == ["post"]
        assert not session.exec(select(AdDirectoryRun)).all()
        assert link.platform_material_id is None
        now = job.due_at


@pytest.mark.parametrize(
    "case", ["missing_ad", "incomplete", "non_spark", "exact_bridge"]
)
def test_actionable_directory_dependency_still_requests_frozen_deleted_read(
    session, sync_seed, monkeypatch, case
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job, link = seed_bridge(
        session, sync_seed, complete=case != "incomplete", spark=case != "non_spark"
    )
    if case == "missing_ad":
        session.delete(
            session.get(AdObject, (sync_seed.tenant_id, "acc-1", "ad", "ad"))
        )
    if case == "exact_bridge":
        link.ad_material_id = "late-inner"
    session.flush()
    assert request_link_directory(session, job, now=datetime.now(UTC)) == 1
    run = session.exec(select(AdDirectoryRun)).one()
    assert run.query["ids"] == ["ad"]
    assert run.query["include_deleted"] is True
    assert run.frozen_route == job.frozen_route


def test_missing_bridge_overview_recheck_does_not_hot_loop(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    job, _ = seed_bridge(session, sync_seed)
    calls = []

    def read(page):
        calls.append(page)
        return overview()

    def no_search(*_args):
        raise AssertionError("已有归属不应再次定位广告")

    def step():
        return refresh_material_links(
            session,
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            read_overview=read,
            read_ad_posts=no_search,
            now=datetime.now(UTC),
            start=D,
            end=D,
            progress=job.progress,
        )

    assert step() and step()
    assert calls == [1], "同一轮完整概览只读一次"
    now = datetime.now(UTC)
    assert wait_for_dependencies(session, job, now=now)
    assert job.due_at > now
    # 调度到期重新建立一轮概览；已有归属仍保留，不重做旧帖子搜索。
    assert step()
    assert calls == [1, 1]
