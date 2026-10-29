"""目录提交后直接推进有界派发，不再等待 Control 的补位消息。"""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import select

from app.core.config import settings
from app.core.db import engine
from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs import outbox
from app.jobs.models import PendingDispatch
from app.modules.ads import directory_dispatch, tasks
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.sync import stage_directory_page
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from tests.modules.ads.test_material_directory_children import _source_page
from tests.modules.reporting.test_jobs import _route


def directory(session, seed, *, terminal=False):
    ref = uuid4().hex
    run = AdDirectoryRun(
        tenant_id=seed.tenant_id,
        advertiser_id="acc-1",
        bc_id="bc-a",
        actor_id=seed.actor_id,
        connection_id=seed.connection.id,
        frozen_route=_route(seed),
        kind="campaign",
        ad_type="REGULAR",
        workload="live",
        partition_key=ref * 2,
        claim_generation=1,
        query={
            "advertiser_id": "acc-1",
            "kind": "campaign",
            "ad_type": "REGULAR",
            "page": 1,
            "page_size": 100,
            "ids": [ref],
            "parent_ids": [],
            "include_deleted": False,
        },
    )
    session.add(run)
    session.flush()
    if terminal:
        stage_directory_page(
            session, run_id=run.id, page=page(complete=True), claim_generation=1
        )
    return run


def page(*, complete):
    return DirectoryPage(
        items=(),
        materials=(),
        next_page=None if complete else 2,
        complete=complete,
        evidence=CallEvidence(request_id="test-request"),
    )


@pytest.fixture(autouse=True)
def enabled(monkeypatch, redis_client):
    del redis_client
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])


@pytest.mark.parametrize("broker_fails", [False, True])
def test_completed_directory_claims_and_sends_next_without_control(
    committed_sync_seed, monkeypatch, redis_client, broker_fails
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        finished = directory(db, seed, terminal=True).id
        waiting = directory(db, seed).id
    sent = []

    def publish(name, **kwargs):
        # 真实第二连接证实发送发生在发布/领取提交后，且不持有窗口锁。
        with factory() as observer:
            row = observer.exec(
                select(AdDirectoryRun)
                .where(AdDirectoryRun.id == finished)
                .with_for_update(nowait=True)
            ).one()
            assert row.status == "COMPLETE" and row.published_version is not None
            assert observer.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:directory-dispatch-window',0))"
                )
            ).scalar_one()
        sent.append((name, kwargs))
        if broker_fails:
            raise ConnectionError("offline broker")

    monkeypatch.setattr(outbox.celery_app, "send_task", publish)
    args = {
        "database_engine": engine,
        "redis_client": redis_client,
        "context": seed.context,
        "run_id": finished,
        "claim_generation": 1,
    }
    assert tasks.run_directory_step(**args) == "READY"
    with factory() as db:
        rows = db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).all()
        assert len(rows) == 1 and rows[0].task_name == "ads.targeted_step"
        assert rows[0].payload["run_id"] == str(waiting)
        assert (rows[0].published_at is None) is broker_fails
        assert rows[0].attempts == 1
    assert len(sent) == 1
    assert tasks.run_directory_step(**args) == "READY"
    assert len(sent) == 1  # 重复终态不能再补位或触发发送扫描。


def test_nonterminal_page_sends_its_continuation_without_scanning_backlog(
    committed_sync_seed, monkeypatch, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identity = directory(db, seed).id
        directory(db, seed)

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(
            ads=SimpleNamespace(read_page=lambda _: page(complete=False))
        )

    scans = []
    original = job_tasks._dispatch_directories

    def track(*args, **kwargs):
        scans.append(True)
        return original(*args, **kwargs)

    sent = []
    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    monkeypatch.setattr(job_tasks, "_dispatch_directories", track)
    monkeypatch.setattr(
        outbox.celery_app, "send_task", lambda name, **kw: sent.append((name, kw))
    )
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=1,
        )
        == "CONTINUE"
    )
    assert not scans
    assert len(sent) == 1 and sent[0][0] == "ads.targeted_step"
    with factory() as db:
        row = db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).one()
        assert row.task_key.endswith(":page:2") and row.published_at is not None


def test_periodic_marker_stays_local_when_reading_and_continuing(
    committed_sync_seed, monkeypatch, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        request_directory(
            db,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            actor_id=seed.actor_id,
            frozen_route=_route(seed),
            advertiser_ids=["acc-1"],
            scope="directory",
            ad_types={"acc-1": {"REGULAR": ("campaign",)}},
            periodic_intervals={"acc-1": timedelta(days=1)},
        )
        identity = db.exec(
            select(AdDirectoryRun.id).where(AdDirectoryRun.tenant_id == seed.tenant_id)
        ).one()
    queries = []

    def read(query):
        queries.append(query)
        assert query.ids == () and query.page == 1 and query.include_deleted
        return page(complete=False)

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=SimpleNamespace(read_page=read))

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    sent = []
    monkeypatch.setattr(
        outbox.celery_app, "send_task", lambda name, **kw: sent.append(name)
    )
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=1,
        )
        == "CONTINUE"
    )
    assert len(queries) == 1 and sent == ["ads.sync_step"]


def test_full_directory_publication_immediately_sends_targeted_material_child(
    committed_sync_seed, monkeypatch, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        source = directory(db, seed)
        source.kind, source.ad_type = "ad", "SMART_PLUS"
        source.query = dict(source.query, kind="ad", ad_type="SMART_PLUS", ids=[])
        assert not directory_dispatch.is_live_directory(source)
        page_seed = SimpleNamespace(
            context=seed.context, ref=SimpleNamespace(advertiser_id="acc-1")
        )
        stage_directory_page(
            db,
            run_id=source.id,
            claim_generation=1,
            page=_source_page(page_seed, 1, [("ad-a", "group-a")], terminal=True),
        )
        identity = source.id
    sent = []
    monkeypatch.setattr(
        outbox.celery_app, "send_task", lambda name, **kw: sent.append((name, kw))
    )
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=1,
            publish_only=True,
        )
        == "READY"
    )
    with factory() as db:
        child = db.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == seed.tenant_id,
                AdDirectoryRun.kind == "creative",
            )
        ).one()
        rows = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
            )
        ).all()
        row = next(row for row in rows if row.payload.get("run_id") == str(child.id))
        assert directory_dispatch.is_live_directory(child)
        assert row.task_name == "ads.targeted_step"
        assert row.published_at is not None
        parent = db.exec(
            select(AdDirectoryRun).where(
                AdDirectoryRun.tenant_id == seed.tenant_id,
                AdDirectoryRun.kind == "adgroup",
            )
        ).one()
        assert any(
            row.payload.get("run_id") == str(parent.id) and row.published_at is not None
            for row in rows
        )
    assert sum(name == "ads.targeted_step" for name, _ in sent) == 2


def test_admission_wait_keeps_delayed_continuation_without_refilling(
    committed_sync_seed, monkeypatch, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identity = directory(db, seed).id
        directory(db, seed)

    @contextmanager
    def deferred(**_kwargs):
        raise AccountAdmissionDeferred(30000)
        yield

    sent = []
    monkeypatch.setattr(tasks, "open_tiktok_gateway", deferred)
    monkeypatch.setattr(outbox.celery_app, "send_task", lambda *a, **k: sent.append(a))
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=1,
        )
        == "WAIT"
    )
    assert not sent
    with factory() as db:
        rows = db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).all()
        assert len(rows) == 1
        assert rows[0].payload == {"run_id": str(identity), "claim_generation": 2}
        run = db.get(AdDirectoryRun, identity)
        assert run.error_code == "admission_deferred"
        assert rows[0].available_at == run.next_attempt_at


def test_failed_publication_commit_does_not_claim_or_send_other_work(
    committed_sync_seed, monkeypatch, redis_client
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identity = directory(db, seed, terminal=True).id
        directory(db, seed)
    publish = tasks.publish_directory

    def rollback(db, **kwargs):
        publish(db, **kwargs)
        db.execute(text("SELECT 1/0"))

    sent = []
    monkeypatch.setattr(tasks, "publish_directory", rollback)
    monkeypatch.setattr(outbox.celery_app, "send_task", lambda *a, **k: sent.append(a))
    with pytest.raises(DBAPIError):
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=1,
        )
    assert not sent
    with factory() as db:
        assert db.get(AdDirectoryRun, identity).published_version is None
        assert not db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).all()


@pytest.mark.parametrize("failure", [RuntimeError, SoftTimeLimitExceeded])
def test_refill_failure_keeps_publication_and_beat_can_resume(
    committed_sync_seed, monkeypatch, redis_client, failure
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identity = directory(db, seed, terminal=True).id
        directory(db, seed)
    original = job_tasks._dispatch_directories

    def unavailable(db, now):
        # 先真实领取，再中断，验证领取事务回滚而业务发布提交不受影响。
        original(db, now)
        raise failure("temporary local query failure")

    monkeypatch.setattr(job_tasks, "_dispatch_directories", unavailable)
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=1,
        )
        == "READY"
    )
    with factory() as db:
        assert db.get(AdDirectoryRun, identity).published_version is not None
    monkeypatch.setattr(job_tasks, "_dispatch_directories", original)
    sent = []
    monkeypatch.setattr(outbox.celery_app, "send_task", lambda *a, **k: sent.append(a))
    assert job_tasks.plan_due.run(payload={"dispatch_only": True}) == 1
    assert sent == [("ads.targeted_step",)]


def test_concurrent_completions_preserve_global_directory_window(
    committed_sync_seed, monkeypatch, redis_client
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(directory_dispatch, "TARGETED_DISPATCH_WINDOW", 8)
    with factory() as db, db.begin():
        completed = [directory(db, seed, terminal=True).id for _ in range(2)]
        waiting = {directory(db, seed).id for _ in range(20)}
    monkeypatch.setattr(outbox.celery_app, "send_task", lambda *a, **k: None)
    barrier = Barrier(2)

    def finish(identity):
        barrier.wait(timeout=5)
        return tasks.run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=seed.context,
            run_id=identity,
            claim_generation=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(finish, completed)) == ["READY", "READY"]
    with factory() as db:
        rows = db.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == seed.tenant_id)
        ).all()
        assert len(rows) == 8
        assert {row.task_name for row in rows} == {"ads.targeted_step"}
        assert {row.payload["run_id"] for row in rows} <= {str(i) for i in waiting}
