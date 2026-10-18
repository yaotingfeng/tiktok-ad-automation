"""真实 PG 验证素材关联每个外部请求之后可重启续步。"""

from uuid import uuid4

from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.jobs import enqueue_job, execute_job
from app.modules.reporting.store_models import MaterialPostLink, ReportSyncJob
from tests.modules.reporting.test_jobs import _route
from tests.modules.reporting.test_material_query import D, material_row, publish


def seed_job(factory, seed):
    with factory() as session:
        publish(session, seed, "acc-1", [material_row("old-post", "5", "same.mp4")])
        session.add_all(
            [
                AdMaterialReference(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    ad_remote_id=f"ad{i:03}",
                    platform_material_id=f"video{i}",
                    ad_material_id=f"material{i}",
                    material_type="VIDEO",
                    name="same.mp4",
                    complete=True,
                    published_version=1,
                )
                for i in range(200)
            ]
        )
        job = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="material_links",
            tier="material",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        session.commit()
        return job


def step(factory, job_id, reader):
    token = uuid4()
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        job.status, job.claim_token = "RUNNING", token
        session.commit()
    reader.calls = 0
    return execute_job(factory, job_id=job_id, claim_token=token, reader=reader)


class SlowReader:
    def __init__(self):
        self.calls = 0
        self.history = []
        self.error = None

    def tick(self, query):
        self.calls += 1
        self.history.append(query)
        if self.calls * 20 > 170:
            raise DomainError("read_deadline_exceeded", "虚拟时钟已超过170秒")
        if self.error is not None:
            raise self.error

    def material_ad_links(self, *, advertiser_id, page):
        self.tick(("overview", page))
        return RawReportPage((), page, 1, 0)

    def material_ad_posts(self, *, advertiser_id, ad_ids, start, end, page):
        assert advertiser_id == "acc-1" and start == end == D
        self.tick((tuple(ad_ids), page))
        mid = "old-post" if page == 2 and "ad199" in ad_ids else "unrelated"
        return RawReportPage(({"dimensions": {"main_material_id": mid}},), page, 2, 2)


def test_slow_binary_search_and_pages_commit_across_worker_steps(committed_sync_seed):
    seed, factory = committed_sync_seed
    job_id, reader = seed_job(factory, seed), SlowReader()
    for _ in range(60):
        result = step(factory, job_id, reader)
        assert reader.calls <= 1, "一步只能发送一次请求，慢响应不能抹掉此前进度"
        with factory() as session:
            job = session.get(ReportSyncJob, job_id)
            assert job.attempts == 0 and job.error_code is None
        if result == "COMPLETE":
            break
    else:
        raise AssertionError("持久化定位不能完成")
    assert reader.history.count(("overview", 1)) == 1
    assert len(reader.history) == len(set(reader.history))
    with factory() as session:
        link = session.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == seed.tenant_id,
                MaterialPostLink.main_material_id == "old-post",
            )
        ).one()
        assert (link.ad_remote_id, link.platform_material_id) == ("ad199", "video199")


def test_completed_overview_and_query_page_survive_later_provider_error(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    job_id, reader = seed_job(factory, seed), SlowReader()
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.history == [("overview", 1)]
    assert step(factory, job_id, reader) == "WAITING"
    with factory() as session:
        saved = session.get(ReportSyncJob, job_id).progress
        assert saved["overview_complete"]
    reader.error = DomainError("report_response_invalid", "平台错误必须保留")
    assert step(factory, job_id, reader) == "FAILED"
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert job.progress == saved and job.error_code == "report_response_invalid"
    reader.error = None
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.history[-1][1] == 2
    assert reader.history.count(("overview", 1)) == 1
