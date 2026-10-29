"""实时状态与身份补录共用32槽，持续旧身份流不能饿死当前状态。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlmodel import select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.ads import directory_dispatch
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from app.modules.reporting.directory_refresh import refresh_report_directory
from tests.modules.reporting.test_directory_direct_delivery import directory
from tests.modules.reporting.test_directory_refresh import prepared

KEY = "directory_service_v1"


def pending(session, seed, service, now, *, old=False, child=False):
    run = directory(session, seed)
    run.created_at = now - timedelta(hours=5 if old else 1)
    run.query = run.query | ({KEY: service} if service else {})
    if child:
        run.query = run.query | {"material_child_v1": {}}
    return run


def sent(session):
    return [
        session.get(AdDirectoryRun, UUID(message.payload["run_id"]))
        for message in session.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_name == "ads.targeted_step"
            )
        ).all()
    ]


@pytest.mark.parametrize("legacy", [False, True])
def test_continuous_old_identity_flow_reserves_half_for_status(
    session, sync_seed, legacy
):
    now = datetime.now(UTC)
    for _ in range(48):
        pending(session, sync_seed, None if legacy else "identity", now, old=True)
    statuses = [pending(session, sync_seed, "status", now) for _ in range(32)]
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 32
    assert len({r.id for r in sent(session)} & {r.id for r in statuses}) == 16
    assert job_tasks._dispatch_directories(session, now) == 0


@pytest.mark.parametrize("occupied", ["status", "identity"])
@pytest.mark.parametrize("inflight", [16, 31])
def test_single_refill_honors_share_with_claims_and_batch_one(
    session, sync_seed, monkeypatch, occupied, inflight
):
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_BATCH", 1)
    for _ in range(inflight):
        run = pending(session, sync_seed, occupied, now, old=True)
        run.status, run.claimed_until = "RUNNING", now + timedelta(minutes=1)
    pending(session, sync_seed, occupied, now, old=True)
    desired = pending(
        session, sync_seed, "identity" if occupied == "status" else "status", now
    )
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 1
    assert [r.id for r in sent(session)] == [desired.id]


@pytest.mark.parametrize("service", ["status", "identity", None])
def test_unused_live_share_is_fully_borrowed(session, sync_seed, service):
    now = datetime.now(UTC)
    for _ in range(33):
        pending(session, sync_seed, service, now)
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 32
    assert job_tasks._dispatch_directories(session, now) == 0


def test_initial_producer_does_not_take_other_services_reserved_slot(
    session, sync_seed
):
    now = datetime.now(UTC)
    for _ in range(31):
        run = pending(session, sync_seed, "identity", now)
        run.status, run.claimed_until = "RUNNING", now + timedelta(minutes=1)
    pending(session, sync_seed, "status", now)
    newcomer = pending(session, sync_seed, "identity", now)
    session.flush()
    directory_dispatch.enqueue_directory_step(session, newcomer, initial=True)
    assert sent(session) == []


@pytest.mark.parametrize("service", [None, "identity"])
def test_hot_fact_reuses_and_promotes_unstarted_old_run(
    session, sync_seed, monkeypatch, service
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(session, sync_seed)
    assert refresh_report_directory(session, job) == 3
    runs = session.exec(select(AdDirectoryRun)).all()
    for run in runs:
        run.query = {k: v for k, v in run.query.items() if k != KEY}
        if service:
            run.query = run.query | {KEY: service}
    session.flush()
    identities = [
        (r.id, r.partition_key, r.claim_generation, r.created_at) for r in runs
    ]
    assert refresh_report_directory(session, job) == 0
    assert [
        (r.id, r.partition_key, r.claim_generation, r.created_at) for r in runs
    ] == identities
    assert all(r.query.get(KEY) == "status" for r in runs)


@pytest.mark.parametrize(
    "guard", ["lease", "token", "page", "outbox", "route", "ids", "child"]
)
def test_hot_demand_does_not_move_inflight_or_unrelated_runs(
    session, sync_seed, monkeypatch, guard
):
    from uuid import uuid4

    from app.modules.ads.sync import stage_directory_page
    from tests.modules.reporting.test_directory_direct_delivery import page

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = prepared(session, sync_seed)
    assert refresh_report_directory(session, job) == 3
    run = session.exec(select(AdDirectoryRun).where(AdDirectoryRun.kind == "ad")).one()
    run.query = run.query | {KEY: "identity"}
    if guard == "lease":
        run.claimed_until = datetime.now(UTC) + timedelta(minutes=1)
    elif guard == "token":
        run.claim_token = uuid4()
    elif guard == "page":
        stage_directory_page(
            session, run_id=run.id, page=page(complete=True), claim_generation=1
        )
    elif guard == "outbox":
        directory_dispatch.enqueue_directory_step(session, run)
    elif guard == "route":
        run.frozen_route = run.frozen_route | {"binding_revision": 17}
    elif guard == "ids":
        run.query = run.query | {"ids": ["unrelated"]}
    else:
        run.query = run.query | {"material_child_v1": {}}
    session.flush()
    refresh_report_directory(session, job)
    assert run.query[KEY] == "identity"


def test_child_always_uses_identity_share_even_with_status_marker(session, sync_seed):
    now = datetime.now(UTC)
    for _ in range(40):
        pending(session, sync_seed, "status", now, old=True, child=True)
    statuses = [pending(session, sync_seed, "status", now) for _ in range(20)]
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 32
    assert len({r.id for r in sent(session)} & {r.id for r in statuses}) == 16


def test_service_shares_keep_tenant_rotation_and_continuous_progress(
    session, sync_seed
):
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    other = seed_fixture.__wrapped__(session)
    now = datetime.now(UTC)
    for seed in (sync_seed, other):
        for _ in range(48):
            pending(session, seed, "identity", now, old=True)
            pending(session, seed, "status", now)
    session.flush()
    for turn in range(3):
        assert (
            job_tasks._dispatch_directories(session, now + timedelta(seconds=turn))
            == 32
        )
        active = [r for r in sent(session) if r.status == "QUEUED"]
        for seed in (sync_seed, other):
            for service in ("status", "identity"):
                assert (
                    sum(
                        r.tenant_id == seed.tenant_id and r.query[KEY] == service
                        for r in active
                    )
                    == 8
                )
        for run in active:
            run.status = "COMPLETE"
        session.flush()


def test_locked_heads_fill_live_shares_and_legacy_payload_stays_unchanged(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        identities = [pending(db, seed, None, now, old=True).id for _ in range(40)]
        statuses = [pending(db, seed, "status", now).id for _ in range(40)]
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(AdDirectoryRun)
            .where(AdDirectoryRun.id.in_(statuses[:20]))
            .with_for_update()
        ).all()
        with factory() as db, db.begin():
            assert job_tasks._dispatch_directories(db, now) == 32
            rows = sent(db)
            assert len({r.id for r in rows} & set(statuses)) == 16
            assert len({r.id for r in rows} & set(identities)) == 16
            for message in db.exec(select(PendingDispatch)).all():
                assert set(message.payload) == {"run_id", "claim_generation"}


@pytest.mark.parametrize("unavailable", ["none", "future", "lease", "outbox"])
def test_initial_producer_borrows_when_other_service_has_no_eligible_work(
    session, sync_seed, unavailable
):
    now = datetime.now(UTC)
    for _ in range(20):
        run = pending(session, sync_seed, "identity", now)
        run.status, run.claimed_until = "RUNNING", now + timedelta(minutes=1)
    if unavailable != "none":
        other = pending(session, sync_seed, "status", now)
        if unavailable == "future":
            other.next_attempt_at = now + timedelta(hours=1)
        elif unavailable == "lease":
            other.claimed_until = now + timedelta(minutes=1)
        else:
            directory_dispatch.enqueue_directory_step(session, other)
    newcomer = pending(session, sync_seed, "identity", now)
    session.flush()
    directory_dispatch.enqueue_directory_step(session, newcomer, initial=True)
    assert newcomer.id in {r.id for r in sent(session)}


def test_initial_borrows_unneeded_part_of_other_share(session, sync_seed):
    now = datetime.now(UTC)
    for _ in range(16):
        run = pending(session, sync_seed, "identity", now)
        run.claimed_until = now + timedelta(minutes=1)
    pending(session, sync_seed, "status", now)
    newcomer = pending(session, sync_seed, "identity", now)
    session.flush()
    assert directory_dispatch.targeted_capacity(session, now, run=newcomer) == 15


@pytest.mark.parametrize("change", ["locked", "generation", "route"])
def test_promotion_rechecks_generation_scope_and_skips_locked_rows(
    committed_sync_seed, change
):
    from app.modules.ads.directory_services import promote_status_requests

    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identity = pending(db, seed, None, datetime.now(UTC)).id
    with factory() as observer, observer.begin():
        old = observer.get(AdDirectoryRun, identity)
        with factory() as writer, writer.begin():
            actual = writer.exec(
                select(AdDirectoryRun)
                .where(AdDirectoryRun.id == identity)
                .with_for_update()
            ).one()
            if change == "generation":
                actual.claim_generation += 1
            elif change == "route":
                actual.frozen_route = actual.frozen_route | {"binding_revision": 8}
            if change == "locked":
                assert promote_status_requests(observer, [old]) == 0
        if change != "locked":
            assert promote_status_requests(observer, [old]) == 0


@pytest.mark.parametrize("service", ["status", "identity"])
def test_service_marker_does_not_change_query_partition(service):
    from app.modules.ads.directory_requests import _partition_key

    query = {"ids": ["one"], "include_deleted": True}
    assert _partition_key(query) == _partition_key(query | {KEY: service})


@pytest.mark.parametrize("service", ["status", "identity", None])
def test_serialized_old_or_marked_task_reads_without_private_metadata(
    committed_sync_seed, monkeypatch, redis_client, service
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.core.db import engine
    from app.jobs import outbox
    from app.modules.ads import tasks
    from tests.modules.reporting.test_directory_direct_delivery import page

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        identity = pending(db, seed, service, datetime.now(UTC)).id
    received = []

    @contextmanager
    def gateway(**_kwargs):
        def read(query):
            received.append(query)
            assert not hasattr(query, KEY)
            return page(complete=False)

        yield SimpleNamespace(ads=SimpleNamespace(read_page=read))

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    monkeypatch.setattr(outbox.celery_app, "send_task", lambda *args, **kwargs: None)
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
    assert len(received) == 1 and received[0].page == 1
    with factory() as db:
        run = db.get(AdDirectoryRun, identity)
        assert run.next_page == 2 and run.query.get(KEY) == service
