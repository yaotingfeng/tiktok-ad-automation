# backend/tests/modules/reporting/test_jobs.py
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.reporting.jobs import (
    claim_jobs,
    enqueue_job,
    execute_job,
    request_time,
)
from app.modules.reporting.store_models import (
    AdReportDay,
    ReportSyncJob,
    ReportSyncState,
)
from tests.modules.reporting.sync_conftest import ad_row

D = date(2026, 10, 5)


class FakeReader:
    def __init__(self, pages=None, error=None, total=None):
        self.pages, self.error, self.total, self.calls = pages or {}, error, total, []

    def ad_day(self, *, advertiser_ids, start, end, page, campaign_ids=()):
        self.calls.append((tuple(advertiser_ids), tuple(campaign_ids), page))
        if self.error:
            raise self.error
        rows = self.pages.get((tuple(advertiser_ids), tuple(campaign_ids), page), [])
        total = self.total(advertiser_ids, campaign_ids) if self.total else len(rows)
        return RawReportPage(tuple(rows), page, 1 if total else 0, total)


def _route(seed):
    return {
        "tenant_id": str(seed.tenant_id),
        "bc_id": "bc-a",
        "connection_id": str(seed.connection.id),
        "channel": "OFFICIAL_API",
        "authorization_revision": 0,
        "adapter_contract_revision": "official-api-v1",
        "binding_revision": 0,
    }


def _claim_one(session):
    jobs = claim_jobs(session, now=datetime.now(UTC) + timedelta(seconds=1), limit=10)
    session.commit()
    return jobs[0]


def _factory(session):
    return lambda: Session(bind=session.get_bind())


def test_enqueue_is_idempotent_while_active(session, sync_seed):
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "ad_day",
        "tier": "hot",
        "advertiser_ids": ["acc-1"],
        "start": D,
        "end": D,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
    }
    assert enqueue_job(session, **args) is not None
    assert enqueue_job(session, **args) is None


def test_execute_publishes_complete(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as s:
        enqueue_job(
            s,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        s.commit()
        job = claim_jobs(s, now=datetime.now(UTC) + timedelta(seconds=1), limit=1)[0]
        s.commit()
    reader = FakeReader(pages={(("acc-1",), (), 1): [ad_row("a1", D)]})
    assert (
        execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader)
        == "COMPLETE"
    )
    with factory() as s:
        assert (
            len(
                s.exec(
                    select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
                ).all()
            )
            == 1
        )


def test_rate_limit_waits_without_counting_attempt(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    reader = FakeReader(error=AccountAdmissionDeferred(300_000))
    assert (
        execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader)
        == "WAITING"
    )
    with factory() as s:
        row = s.get(ReportSyncJob, job.id)
        assert row.attempts == 0 and row.due_at > datetime.now(UTC) + timedelta(
            seconds=290
        )


def test_provider_errors_back_off_then_fail(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    reader = FakeReader(error=DomainError("tiktok_response_error", "x"))
    for _attempt in range(1, 6):
        assert (
            execute_job(
                factory, job_id=job.id, claim_token=job.claim_token, reader=reader
            )
            == "WAITING"
        )
        job = _reclaim(factory, job.id)
    assert (
        execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader)
        == "FAILED"
    )
    with factory() as s:
        assert (
            s.get(ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", D)).status
            == "FAILED"
        )


def test_contract_error_fails_immediately(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    reader = FakeReader(error=DomainError("report_query_invalid", "x"))
    assert (
        execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader)
        == "FAILED"
    )


def test_truncation_splits_then_fails_visibly(committed_sync_seed):
    seed, factory = committed_sync_seed
    # 两个账户合并触顶 → 拆成单账户；单账户仍触顶且已有系列 ID → 按系列分批；系列批仍触顶 → 失败
    job = _enqueue_and_claim(factory, seed, advertisers=["acc-1", "acc-2"])
    reader = FakeReader(total=lambda advs, camps: 20_000)
    assert (
        execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader)
        == "SPLIT"
    )
    with factory() as s:
        children = s.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.status == "QUEUED",
            )
        ).all()
        assert sorted(c.advertiser_ids for c in children) == [["acc-1"], ["acc-2"]]
        assert all(request_time(child) == request_time(job) for child in children)
        assert all(child.created_at >= job.created_at for child in children)
        assert s.get(ReportSyncJob, job.id).status == "COMPLETE"


def _enqueue_and_claim(factory, seed, advertisers=None):
    with factory() as s:
        enqueue_job(
            s,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="hot",
            advertiser_ids=advertisers or ["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        s.commit()
        job = claim_jobs(s, now=datetime.now(UTC) + timedelta(seconds=1), limit=1)[0]
        s.commit()
        return job


def _reclaim(factory, job_id):
    with factory() as s:
        job = s.get(ReportSyncJob, job_id)
        job.due_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
        result = claim_jobs(s, now=datetime.now(UTC), limit=1)[0]
        s.commit()
        return result


def test_first_truncation_discovers_campaigns_and_publishes_whole_account(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)

    class Reader(FakeReader):
        def campaign_day(self, **_kwargs):
            return RawReportPage(
                tuple(
                    {"dimensions": {"campaign_id": f"c{i}"}, "metrics": {}}
                    for i in range(31)
                ),
                1,
                1,
                31,
            )

        def ad_day(self, *, advertiser_ids, start, end, page, campaign_ids=()):
            if not campaign_ids:
                return RawReportPage((), 1, 20, 20_000)
            assert len(campaign_ids) <= 30
            return RawReportPage(
                tuple(ad_row(f"a{c}", D, campaign=c) for c in campaign_ids),
                1,
                1,
                len(campaign_ids),
            )

    assert (
        execute_job(
            factory, job_id=job.id, claim_token=job.claim_token, reader=Reader()
        )
        == "COMPLETE"
    )
    with factory() as s:
        rows = s.exec(
            select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
        ).all()
        assert len(rows) == 31
        assert (
            s.get(ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", D)).row_count
            == 31
        )
        assert s.get(ReportSyncJob, job.id).request_count == 4


def test_campaign_batch_still_truncated_preserves_all_old_rows(committed_sync_seed):
    from uuid import uuid4

    from app.modules.reporting.publish import AccountMeta, publish_ad_day

    seed, factory = committed_sync_seed
    with factory() as s:
        publish_ad_day(
            s,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "UTC")},
            start=D,
            end=D,
            rows=[ad_row("old", D)],
        )
        s.commit()
    job = _enqueue_and_claim(factory, seed)

    class Reader(FakeReader):
        def campaign_day(self, **_kwargs):
            return RawReportPage(({"dimensions": {"campaign_id": "c1"}},), 1, 1, 1)

    assert (
        execute_job(
            factory,
            job_id=job.id,
            claim_token=job.claim_token,
            reader=Reader(total=lambda _a, _c: 20_000),
        )
        == "FAILED"
    )
    with factory() as s:
        assert [
            r.ad_id
            for r in s.exec(
                select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
            ).all()
        ] == ["old"]
        assert (
            s.get(ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", D)).status
            == "STALE"
        )


def test_expired_lease_is_reclaimed_and_old_token_cannot_publish(committed_sync_seed):
    seed, factory = committed_sync_seed
    old = _enqueue_and_claim(factory, seed)
    with factory() as s:
        job = s.get(ReportSyncJob, old.id)
        job.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
        claimed = claim_jobs(s, now=datetime.now(UTC), limit=1)
        assert len(claimed) == 1 and claimed[0].claim_token != old.claim_token
        s.commit()
    reader = FakeReader()
    assert (
        execute_job(factory, job_id=old.id, claim_token=old.claim_token, reader=reader)
        == "FAILED"
    )
    assert reader.calls == []


def test_same_accounts_in_different_tenants_do_not_share_job_key(session, sync_seed):
    from uuid import uuid4

    args = {
        "bc_id": "bc-a",
        "kind": "ad_day",
        "tier": "hot",
        "advertiser_ids": ["acc-1"],
        "start": D,
        "end": D,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
    }
    assert enqueue_job(session, tenant_id=sync_seed.tenant_id, **args)
    assert enqueue_job(session, tenant_id=uuid4(), **args)


def test_timeout_retry_reads_campaign_batches_instead_of_same_large_request(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    assert (
        execute_job(
            factory,
            job_id=job.id,
            claim_token=job.claim_token,
            reader=FakeReader(error=DomainError("report_timeout", "超时")),
        )
        == "WAITING"
    )
    job = _reclaim(factory, job.id)

    class Reader(FakeReader):
        def campaign_day(self, **_kwargs):
            return RawReportPage(({"dimensions": {"campaign_id": "c1"}},), 1, 1, 1)

        def ad_day(self, *, campaign_ids=(), **_kwargs):
            assert campaign_ids == ("c1",)
            return RawReportPage((ad_row("a1", D),), 1, 1, 1)

    assert (
        execute_job(
            factory, job_id=job.id, claim_token=job.claim_token, reader=Reader()
        )
        == "COMPLETE"
    )


def test_failure_on_later_page_keeps_previous_publication(committed_sync_seed):
    from uuid import uuid4

    from app.modules.reporting.publish import AccountMeta, publish_ad_day

    seed, factory = committed_sync_seed
    with factory() as s:
        publish_ad_day(
            s,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "UTC")},
            start=D,
            end=D,
            rows=[ad_row("old", D)],
        )
        s.commit()
    job = _enqueue_and_claim(factory, seed)

    class Reader(FakeReader):
        def ad_day(self, *, page, **_kwargs):
            if page == 2:
                raise DomainError("tiktok_response_error", "中断")
            return RawReportPage((ad_row("new", D),), 1, 2, 2)

    assert (
        execute_job(
            factory, job_id=job.id, claim_token=job.claim_token, reader=Reader()
        )
        == "WAITING"
    )
    with factory() as s:
        assert [
            r.ad_id
            for r in s.exec(
                select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
            ).all()
        ] == ["old"]
        assert s.get(ReportSyncJob, job.id).request_count == 2


def test_split_discovers_new_campaigns_despite_old_cache(committed_sync_seed):
    from uuid import uuid4

    from app.modules.reporting.publish import AccountMeta, publish_ad_day

    seed, factory = committed_sync_seed
    with factory() as s:
        publish_ad_day(
            s,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "UTC")},
            start=D,
            end=D,
            rows=[ad_row("old", D)],
        )
        s.commit()
    job = _enqueue_and_claim(factory, seed)

    calls = []

    class Reader(FakeReader):
        def campaign_day(self, *, start, end, **_kwargs):
            assert (start, end) == (D, D)
            return RawReportPage(
                tuple({"dimensions": {"campaign_id": c}} for c in ("c1", "c2")), 1, 1, 2
            )

        def ad_day(self, *, campaign_ids=(), **_kwargs):
            if not campaign_ids:
                return RawReportPage((), 1, 20, 20_000)
            calls.extend(campaign_ids)
            return RawReportPage(
                tuple(ad_row(f"new-{c}", D, campaign=c) for c in campaign_ids),
                1,
                1,
                len(campaign_ids),
            )

    assert (
        execute_job(
            factory, job_id=job.id, claim_token=job.claim_token, reader=Reader()
        )
        == "COMPLETE"
    )
    assert calls == ["c1", "c2"]
    with factory() as s:
        assert sorted(
            r.ad_id
            for r in s.exec(
                select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)
            ).all()
        ) == ["new-c1", "new-c2"]


@pytest.mark.parametrize("code", ["report_timeout", "read_deadline_exceeded"])
def test_split_timeout_eventually_fails(committed_sync_seed, code):
    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)

    class Reader(FakeReader):
        def campaign_day(self, **_kwargs):
            return RawReportPage(({"dimensions": {"campaign_id": "c1"}},), 1, 1, 1)

        def ad_day(self, *, campaign_ids=(), **_kwargs):
            if not campaign_ids:
                return RawReportPage((), 1, 20, 20_000)
            raise DomainError(code, "读取超时")

    for attempt in range(1, 7):
        result = execute_job(
            factory, job_id=job.id, claim_token=job.claim_token, reader=Reader()
        )
        assert result == ("WAITING" if attempt <= 5 else "FAILED")
        with factory() as s:
            assert s.get(ReportSyncJob, job.id).attempts == min(attempt, 5)
        if attempt <= 5:
            job = _reclaim(factory, job.id)
    with factory() as s:
        assert s.get(
            ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", D)
        ).status in ("FAILED", "STALE")


def test_material_fact_commits_before_separately_claimed_link_job(committed_sync_seed):
    # 素材金额先提交，再正常领取独立关联任务；概览读取不阻挡事实。
    from app.modules.reporting.store_models import MaterialPostLink, MaterialReportDay

    seed, factory = committed_sync_seed

    class Reader(FakeReader):
        def material_day(self, *, advertiser_id, start, end, page):
            row = {
                "dimensions": {
                    "main_material_id": "p1",
                    "main_material_type": "VIDEO_SPARK_ADS",
                    "stat_time_day": str(D),
                },
                "metrics": {"spend": "3"},
            }
            return RawReportPage((row,), 1, 1, 1)

        def material_ad_links(self, *, advertiser_id, page):
            self.calls.append(("links", advertiser_id, page))
            row = {
                "dimensions": {
                    "smart_plus_ad_id": "a1",
                    "main_material_id": "p1",
                    "main_material_type": "VIDEO_SPARK_ADS",
                },
                "metrics": {"ad_material_id": "m1"},
            }
            return RawReportPage((row,), 1, 1, 1)

    with factory() as s:
        enqueue_job(
            s,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="material_day",
            tier="material",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        s.commit()
        job = claim_jobs(s, now=datetime.now(UTC) + timedelta(seconds=1), limit=1)[0]
        s.commit()
    reader = Reader()
    assert (
        execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader)
        == "COMPLETE"
    )
    assert reader.calls == []
    with factory() as s:
        assert s.get(ReportSyncJob, job.id).status == "COMPLETE"
        fact = s.get(MaterialReportDay, (seed.tenant_id, "acc-1", D, "p1"))
        assert fact is not None and fact.spend == 3
        assert (
            s.exec(
                select(MaterialPostLink).where(
                    MaterialPostLink.tenant_id == seed.tenant_id
                )
            ).all()
            == []
        )
        linked_jobs = claim_jobs(
            s, now=datetime.now(UTC) + timedelta(seconds=1), limit=10
        )
        assert len(linked_jobs) == 1 and linked_jobs[0].kind == "material_links"
        link_job = linked_jobs[0]
        s.commit()
    assert (
        execute_job(
            factory, job_id=link_job.id, claim_token=link_job.claim_token, reader=reader
        )
        == "COMPLETE"
    )
    assert reader.calls == [("links", "acc-1", 1)]
    with factory() as s:
        link = s.exec(
            select(MaterialPostLink).where(MaterialPostLink.tenant_id == seed.tenant_id)
        ).one()
        assert (link.ad_remote_id, link.ad_material_id) == ("a1", "m1")


def test_hot_report_queues_missing_ad_and_parent_directory(
    session, sync_seed, monkeypatch
):
    from app.core.config import settings
    from app.jobs.models import PendingDispatch
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting.jobs import _CountingReader, _run

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    today = datetime.now(UTC).date()
    job_id = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        tier="hot",
        advertiser_ids=["acc-2"],
        start=today,
        end=today,
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
    )
    job = session.get(ReportSyncJob, job_id)
    reader = FakeReader(
        pages={(("acc-2",), (), 1): [ad_row("new-ad", today, adv="acc-2", spend="30")]}
    )
    assert _run(session, job, _CountingReader(reader)) == 1
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {r.kind for r in runs} == {"campaign", "adgroup", "ad"}
    assert all(
        r.ad_type == "SMART_PLUS" and (r.query["ids"] or r.query["parent_ids"])
        for r in runs
    )
    dispatches = session.exec(
        select(PendingDispatch).where(PendingDispatch.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(dispatches) == 3 and all(
        r.task_name == "ads.targeted_step" for r in dispatches
    )
    # 完整新一轮热刷仍合并未完成的回读，不靠重放同一旧水位任务验证去重。
    job.status = "COMPLETE"
    session.flush()
    newer_id = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        tier="hot",
        advertiser_ids=["acc-2"],
        start=today,
        end=today,
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
    )
    newer = session.get(ReportSyncJob, newer_id)
    assert _run(session, newer, _CountingReader(reader)) == 1
    assert (
        len(
            session.exec(
                select(AdDirectoryRun).where(
                    AdDirectoryRun.tenant_id == sync_seed.tenant_id
                )
            ).all()
        )
        == 3
    )


def test_old_cross_day_hot_job_cannot_deduplicate_or_occupy_current_day_lane(
    session, sync_seed
):
    """旧版昨天至今天任务转冷后，新的当天请求必须仍能独立规划和领取。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    kwargs = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "kind": "ad_day",
        "tier": "hot",
        "advertiser_ids": ["acc-1"],
        "end": today,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "due_at": now,
    }
    old_id = enqueue_job(session, start=today - timedelta(days=1), **kwargs)
    current_id = enqueue_job(session, start=today, **kwargs)
    assert old_id is not None and current_id is not None
    current = claim_jobs(session, now=now, limit=10, lane="live")
    old = claim_jobs(session, now=now, limit=10, lane="backfill")
    assert [job.id for job in current] == [current_id]
    assert [job.id for job in old] == [old_id]


@pytest.mark.parametrize("old_rows", [[], [ad_row("a1", D, spend="1")]])
def test_split_child_preserves_hot_publish_after_parent_was_requested(
    committed_sync_seed, old_rows
):
    """父请求等待时新热刷先完成，稍后生成的子任务也必须沿用父请求水位。"""
    from uuid import uuid4

    from app.modules.reporting.publish import AccountMeta, publish_ad_day

    seed, factory = committed_sync_seed
    parent = _enqueue_and_claim(factory, seed, advertisers=["acc-1", "acc-2"])
    with factory() as session:
        publish_ad_day(
            session,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
            start=D,
            end=D,
            rows=[ad_row("a1", D, spend="20")],
        )
        successful_at = session.get(
            ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", D)
        ).last_success_at
        session.commit()
    assert (
        execute_job(
            factory,
            job_id=parent.id,
            claim_token=parent.claim_token,
            reader=FakeReader(total=lambda _advs, _camps: 20_000),
        )
        == "SPLIT"
    )
    with factory() as session:
        children = claim_jobs(
            session, now=datetime.now(UTC) + timedelta(seconds=1), limit=10
        )
        child = next(job for job in children if job.advertiser_ids == ["acc-1"])
        assert child.created_at > successful_at > request_time(child)
        session.commit()
    assert (
        execute_job(
            factory,
            job_id=child.id,
            claim_token=child.claim_token,
            reader=FakeReader(pages={(("acc-1",), (), 1): old_rows}),
        )
        == "COMPLETE"
    )
    with factory() as session:
        assert session.get(AdReportDay, (seed.tenant_id, "acc-1", D, "a1")).spend == 20
        assert (
            session.get(
                ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", D)
            ).last_success_at
            == successful_at
        )


def test_admission_retry_keeps_original_fact_place_but_obeys_not_before(
    session, sync_seed
):
    from zoneinfo import ZoneInfo

    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "tier": "hot",
        "advertiser_ids": ["acc-1"],
        "start": today,
        "end": today,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
    }
    old = enqueue_job(
        session, **common, kind="material_day", due_at=now + timedelta(seconds=5)
    )
    new = enqueue_job(
        session, **common, kind="ad_day", due_at=now - timedelta(seconds=30)
    )
    session.get(ReportSyncJob, old).created_at = now - timedelta(minutes=5)
    session.get(ReportSyncJob, old).status = "WAITING"
    session.flush()
    assert [job.id for job in claim_jobs(session, now=now, limit=1, lane="live")] == [
        new
    ]
    session.get(ReportSyncJob, new).status = "QUEUED"
    session.get(ReportSyncJob, new).claim_token = None
    session.get(ReportSyncJob, new).claimed_until = None
    session.flush()
    # 到准入时间后，老素材请求先于新广告领取，不因修改 due_at 被反复推后。
    assert [
        job.id
        for job in claim_jobs(
            session, now=now + timedelta(seconds=6), limit=1, lane="live"
        )
    ] == [old]


def _live_kind_backlog(session, seed, now, kinds=("ad_day", "material_day")):
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess

    accounts = [f"kind-account-{index}" for index in range(8)]
    for advertiser in accounts:
        session.add(
            AdvertiserAccount(
                tenant_id=seed.tenant_id,
                advertiser_id=advertiser,
                currency="USD",
                timezone="UTC",
                name=advertiser,
            )
        )
    session.flush()
    for advertiser in accounts:
        session.add(
            BCAccountAccess(
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                advertiser_id=advertiser,
                connection_id=seed.connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=now,
            )
        )
    session.flush()
    result = {}
    for rank, kind in enumerate(kinds):
        result[kind] = []
        for index, advertiser in enumerate(accounts):
            job_id = enqueue_job(
                session,
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                kind=kind,
                tier="hot",
                advertiser_ids=[advertiser],
                start=now.date(),
                end=now.date(),
                frozen_route=_route(seed),
                actor_id=seed.actor_id,
                due_at=now,
            )
            # 整批广告比全部素材都老，复现跨kind FIFO成波领取。
            session.get(ReportSyncJob, job_id).created_at = now - timedelta(
                minutes=20 - rank, seconds=8 - index
            )
            result[kind].append(job_id)
    session.flush()
    return result


def test_live_batch_balances_kinds_with_virtual_inflight(session, sync_seed):
    now = datetime.now(UTC)
    ids = _live_kind_backlog(session, sync_seed, now)
    claimed = claim_jobs(session, now=now, limit=8, lane="live")
    assert [job.kind for job in claimed] == ["ad_day", "material_day"] * 4
    for kind in ids:
        assert [job.id for job in claimed if job.kind == kind] == ids[kind][:4]


def test_single_fact_refill_replenishes_fast_kind_before_older_other_kind(
    session, sync_seed
):
    now = datetime.now(UTC)
    ids = _live_kind_backlog(session, sync_seed, now)
    initial = claim_jobs(session, now=now, limit=8, lane="live")
    assert sum(job.kind == "material_day" for job in initial) == 4
    for index in range(4):
        finished = session.get(ReportSyncJob, ids["material_day"][index])
        finished.status, finished.claim_token, finished.claimed_until = (
            "COMPLETE",
            None,
            None,
        )
        session.flush()
        claimed = claim_jobs(session, now=now, limit=1, lane="live")
        assert [job.id for job in claimed] == [ids["material_day"][index + 4]]
        assert (
            sum(
                session.get(ReportSyncJob, job_id).status == "RUNNING"
                for job_id in ids["ad_day"]
            )
            == 4
        )


@pytest.mark.parametrize("kind", ["ad_day", "material_day"])
def test_single_available_fact_kind_borrows_entire_window(session, sync_seed, kind):
    now = datetime.now(UTC)
    ids = _live_kind_backlog(session, sync_seed, now, kinds=(kind,))
    assert [
        job.id for job in claim_jobs(session, now=now, limit=8, lane="live")
    ] == ids[kind]


def test_expired_fact_claims_do_not_count_as_kind_inflight(session, sync_seed):
    from uuid import uuid4

    now = datetime.now(UTC)
    ids = _live_kind_backlog(session, sync_seed, now)
    old_claim = uuid4()
    for job_id in ids["material_day"]:
        row = session.get(ReportSyncJob, job_id)
        row.status, row.claim_token, row.claimed_until = (
            "RUNNING",
            old_claim,
            now - timedelta(seconds=1),
        )
    session.flush()
    claimed = claim_jobs(session, now=now, limit=8, lane="live")
    assert [job.kind for job in claimed] == ["ad_day", "material_day"] * 4
    assert all(
        job.claim_token != old_claim and job.claimed_until > now for job in claimed
    )
