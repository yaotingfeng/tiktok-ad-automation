"""生产尾部缺口：缺广告本体也必须补读，历史请求不能漏掉已删除对象。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.directory_refresh import (
    refresh_report_directory,
    request_link_directory,
)
from app.modules.reporting.material_links import refresh_material_links
from app.modules.reporting.store_models import MaterialPostLink, ReportSyncJob
from tests.modules.reporting.test_backlog_control import request
from tests.modules.reporting.test_directory_refresh import prepared
from tests.modules.reporting.test_material_links import overview
from tests.modules.reporting.test_material_query import D, material_row, publish, use


def test_partial_overview_repairs_known_directory_before_remaining_search(
    committed_sync_seed, monkeypatch
):
    from tests.modules.reporting.test_material_link_resume import step

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    with factory() as session:
        publish(
            session,
            seed,
            "acc-1",
            [
                material_row("known", "5", "known.mp4"),
                material_row("old", "2", "old.mp4"),
            ],
        )
        job_id = request(session, seed, D)
        session.commit()

    class Reader:
        def material_ad_links(self, *, advertiser_id, page, ad_ids=()):
            assert advertiser_id == "acc-1"
            # 第一页证明一个广告身份，后续概览和旧帖定位尚未完成。
            rows = overview(("known-ad", "known", "material", "known.mp4")).rows
            return RawReportPage(rows, page, 3, 3)

    reader = Reader()
    assert step(factory, job_id, reader) == "WAITING"
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert job.progress["next_page"] == 2
        assert not job.progress.get("overview_complete")
        runs = session.exec(select(AdDirectoryRun)).all()
        assert len(runs) == 1, "已确认的目录依赖不能等待整账户搜索结束才发起"
        assert runs[0].query["ids"] == ["known-ad"]
        assert runs[0].query["include_deleted"] is True
        assert runs[0].frozen_route == job.frozen_route

    # 下一页再次返回同一广告时，保留原补读，不能每页制造重复请求。
    assert step(factory, job_id, reader) == "WAITING"
    with factory() as session:
        assert len(session.exec(select(AdDirectoryRun)).all()) == 1
        job = session.get(ReportSyncJob, job_id)
        assert job.progress["next_page"] == 3
        assert not job.progress.get("overview_complete")


@pytest.mark.parametrize("has_ad", [False, True])
def test_link_repair_reads_deleted_ads_even_without_local_object(
    session, sync_seed, monkeypatch, has_ad
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    publish(session, sync_seed, "acc-1", [material_row("p", "5", "file.mp4")])
    if has_ad:
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                kind="ad",
                remote_id="ad",
                ad_type="SMART_PLUS",
                observed_at=now,
                published_version=1,
            )
        )
    session.add(
        MaterialPostLink(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            main_material_id="p",
            ad_remote_id="ad",
            main_material_type="VIDEO_SPARK_ADS",
            observed_at=now,
        )
    )
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    session.flush()
    assert request_link_directory(session, job, now=now) == 1
    run = session.exec(select(AdDirectoryRun)).one()
    assert run.ad_type == "SMART_PLUS"
    assert run.query["ids"] == ["ad"]
    assert run.query["include_deleted"] is True
    assert run.advertiser_id == "acc-1" and run.frozen_route == job.frozen_route
    assert request_link_directory(session, job, now=now) == 0


def test_live_only_read_does_not_suppress_deleted_ad_repair(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    publish(session, sync_seed, "acc-1", [material_row("p", "5", "file.mp4")])
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="ad",
            remote_id="ad",
            ad_type="SMART_PLUS",
            observed_at=now,
            published_version=1,
        )
    )
    session.add(
        MaterialPostLink(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            main_material_id="p",
            ad_remote_id="ad",
            main_material_type="VIDEO_SPARK_ADS",
            observed_at=now,
        )
    )
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    session.flush()
    assert request_link_directory(session, job, now=now) == 1
    old = session.exec(select(AdDirectoryRun)).one()
    old.query = old.query | {"include_deleted": False}
    old.status = "COMPLETE"
    session.flush()
    assert request_link_directory(session, job, now=now) == 1


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
    # 规划提前量与完成余量分开：等待中的35分钟边界健康，超过仍须告警。
    assert (
        measure_health(session, plan, now=now + timedelta(minutes=9))[0][
            "missing_directory_ads"
        ]
        == 0
    )
    assert (
        measure_health(session, plan, now=now + timedelta(minutes=10))[0][
            "missing_directory_ads"
        ]
        == 1
    )


def test_spending_unresolved_post_is_located_before_zero_spend_history(
    session, sync_seed
):
    publish(
        session,
        sync_seed,
        "acc-1",
        [
            material_row("a-zero", "0", "zero.mp4"),
            material_row("z-spend", "20", "paid.mp4"),
        ],
    )
    for ad, name in [("zero-ad", "zero.mp4"), ("paid-ad", "paid.mp4")]:
        use(session, sync_seed, adv="acc-1", ad=ad, video=ad, ad_material=ad)
        ref = session.exec(
            select(AdMaterialReference).where(AdMaterialReference.ad_remote_id == ad)
        ).one()
        ref.name = name
    session.flush()

    def read_posts(ads, _start, _end, _page):
        post = "z-spend" if ads == ("paid-ad",) else "a-zero"
        return RawReportPage(({"dimensions": {"main_material_id": post}},), 1, 1, 1)

    refresh_material_links(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        read_overview=lambda page: overview(),
        read_ad_posts=read_posts,
        now=datetime.now(UTC),
        start=D,
        end=D,
        progress={"overview_complete": True},
    )
    paid = session.get(
        MaterialPostLink, (sync_seed.tenant_id, "acc-1", "z-spend", "paid-ad")
    )
    assert paid is not None and paid.ad_remote_id == "paid-ad"
    assert paid.platform_material_id is None  # 名称只用于搜索候选，不证明历史视频。
    assert (
        session.get(
            MaterialPostLink, (sync_seed.tenant_id, "acc-1", "a-zero", "zero-ad")
        )
        is None
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


@pytest.mark.parametrize(
    "case",
    [
        "exact",
        "name_only",
        "ambiguous",
        "post_id",
        "complete_post_id",
        "other_ad",
        "other_account",
        "regular_complete",
        "regular_incomplete",
    ],
)
def test_incomplete_directory_requires_unique_exact_video_identity(
    session, sync_seed, case
):
    from app.modules.reporting.material_links import _resolve_videos
    from tests.modules.reporting.test_material_query import material_row, publish

    # 精确身份回归针对有金额的帖子；无金额帖子不再参与补链。
    publish(session, sync_seed, "acc-1", [material_row("post", "1")])
    link = MaterialPostLink(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        main_material_id="post",
        ad_remote_id="ad",
        main_material_type="VIDEO"
        if case.startswith("regular_")
        else "VIDEO_SPARK_ADS",
        ad_material_id=None if case == "name_only" else "material",
        material_name="file.mp4",
        observed_at=datetime.now(UTC),
    )
    session.add(link)
    for video in ["v1", "v2"] if case == "ambiguous" else ["v1"]:
        session.add(
            AdMaterialReference(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2" if case == "other_account" else "acc-1",
                ad_remote_id="other-ad" if case == "other_ad" else "ad",
                ad_material_id="material",
                platform_material_id=video,
                material_type="TIKTOK_POST"
                if case in {"post_id", "complete_post_id"}
                else "VIDEO",
                name="file.mp4",
                complete=case in {"complete_post_id", "regular_complete"},
                published_version=1,
            )
        )
    session.flush()
    _resolve_videos(session, sync_seed.tenant_id, "acc-1")
    session.refresh(link)
    assert link.platform_material_id == (
        "v1" if case in {"exact", "regular_complete"} else None
    )
    assert any(session.exec(select(AdMaterialReference.complete)).all()) == (
        case in {"complete_post_id", "regular_complete"}
    )
