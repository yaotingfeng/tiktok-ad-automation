"""缺失与普通状态刷新沿同一服务持久轮转，不改变平台请求。"""

from datetime import UTC, datetime, timedelta

from app.modules.ads import directory_dispatch
from app.modules.ads.directory_services import directory_service
from app.modules.reporting import job_tasks
from tests.modules.reporting.test_directory_service_share import pending, sent


def test_batch_one_remembers_last_completed_status_class(
    session, sync_seed, monkeypatch
):
    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_BATCH", 1)
    for _ in range(4):
        pending(session, sync_seed, "status", now, old=True)
        pending(session, sync_seed, "status_missing", now)
    session.flush()
    classes = []
    for turn in range(6):
        assert (
            job_tasks._dispatch_directories(session, now + timedelta(seconds=turn)) == 1
        )
        active = [r for r in sent(session) if r.status == "QUEUED"]
        assert len(active) == 1
        classes.append(active[0].query["directory_service_v1"])
        active[0].status = "COMPLETE"
        session.flush()
    assert classes == ["status_missing", "status"] * 3


def test_missing_and_regular_share_status_half_of_32(session, sync_seed):
    now = datetime.now(UTC)
    for _ in range(40):
        for kind in ("status", "status_missing", "identity"):
            pending(session, sync_seed, kind, now, old=kind != "status_missing")
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 32
    classes = [r.query["directory_service_v1"] for r in sent(session)]
    assert {kind: classes.count(kind) for kind in set(classes)} == {
        "status": 8,
        "status_missing": 8,
        "identity": 16,
    }


def test_initial_status_cannot_jump_other_class_turn(session, sync_seed):
    now = datetime.now(UTC)
    pending(session, sync_seed, "status_missing", now)
    newcomer = pending(session, sync_seed, "status", now, old=True)
    session.flush()
    directory_dispatch.enqueue_directory_step(session, newcomer, initial=True)
    assert sent(session) == []


def test_missing_class_is_status_and_keeps_partition():
    from types import SimpleNamespace

    from app.modules.ads.directory_requests import _partition_key

    query = {"ids": ["one"], "include_deleted": False}
    marked = query | {"directory_service_v1": "status_missing"}
    assert directory_service(SimpleNamespace(query=marked)) == "status"
    assert _partition_key(marked) == _partition_key(query)


def test_immediate_admission_remembers_turn_but_page_continuation_does_not(
    session, sync_seed
):
    from app.modules.reporting.store_models import DirectoryDispatchCursor

    now = datetime.now(UTC)
    regular = pending(session, sync_seed, "status", now)
    missing = pending(session, sync_seed, "status_missing", now)
    other_missing = pending(session, sync_seed, "status_missing", now)
    session.flush()
    directory_dispatch.enqueue_directory_step(session, missing, initial=True)
    assert {r.id for r in sent(session)} == {missing.id}
    assert (
        session.get(DirectoryDispatchCursor, sync_seed.tenant_id).last_status_missing
        is True
    )
    directory_dispatch.enqueue_directory_step(session, other_missing, initial=True)
    assert {r.id for r in sent(session)} == {missing.id}
    directory_dispatch.enqueue_directory_step(session, regular, initial=True)
    session.expire_all()
    assert (
        session.get(DirectoryDispatchCursor, sync_seed.tenant_id).last_status_missing
        is False
    )
    missing.next_page = 2
    session.flush()
    directory_dispatch.enqueue_directory_step(session, missing)
    session.expire_all()
    assert (
        session.get(DirectoryDispatchCursor, sync_seed.tenant_id).last_status_missing
        is False
    )


def test_rollback_does_not_consume_status_turn(session, sync_seed, monkeypatch):
    from app.modules.reporting.store_models import DirectoryDispatchCursor

    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_BATCH", 1)
    pending(session, sync_seed, "status", now, old=True)
    missing = pending(session, sync_seed, "status_missing", now)
    session.flush()
    savepoint = session.begin_nested()
    assert job_tasks._dispatch_directories(session, now) == 1
    assert (
        session.get(DirectoryDispatchCursor, sync_seed.tenant_id).last_status_missing
        is True
    )
    savepoint.rollback()
    session.expire_all()
    assert session.get(DirectoryDispatchCursor, sync_seed.tenant_id) is None
    assert sent(session) == []
    assert job_tasks._dispatch_directories(session, now) == 1
    assert [r.id for r in sent(session)] == [missing.id]


def test_three_status_classes_preserve_tenant_shares(session, sync_seed):
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    other = seed_fixture.__wrapped__(session)
    now = datetime.now(UTC)
    for seed in (sync_seed, other):
        for _ in range(24):
            for kind in ("status", "status_missing", "identity"):
                pending(session, seed, kind, now, old=kind != "status_missing")
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 32
    for seed in (sync_seed, other):
        classes = [
            r.query["directory_service_v1"]
            for r in sent(session)
            if r.tenant_id == seed.tenant_id
        ]
        assert {kind: classes.count(kind) for kind in set(classes)} == {
            "status": 4,
            "status_missing": 4,
            "identity": 8,
        }


def test_only_missing_class_borrows_entire_window(session, sync_seed):
    from app.modules.reporting.store_models import DirectoryDispatchCursor

    now = datetime.now(UTC)
    for _ in range(33):
        pending(session, sync_seed, "status_missing", now)
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 32
    assert job_tasks._dispatch_directories(session, now) == 0
    assert (
        session.get(DirectoryDispatchCursor, sync_seed.tenant_id).last_status_missing
        is True
    )


def test_locked_missing_head_yields_to_regular_and_refills(committed_sync_seed):
    from sqlmodel import select

    from app.modules.ads.sync_models import AdDirectoryRun

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        missing = pending(db, seed, "status_missing", now).id
        regular_ids = [pending(db, seed, "status", now).id for _ in range(32)]
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.id == missing).with_for_update()
        ).one()
        with factory() as db, db.begin():
            assert job_tasks._dispatch_directories(db, now) == 32
            assert {r.id for r in sent(db)} == set(regular_ids)


def test_initial_ignores_locked_other_class(committed_sync_seed):
    from sqlmodel import select

    from app.modules.ads.sync_models import AdDirectoryRun

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as db, db.begin():
        missing = pending(db, seed, "status_missing", now).id
        regular = pending(db, seed, "status", now).id
    with factory() as blocker, blocker.begin():
        blocker.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.id == missing).with_for_update()
        ).one()
        with factory() as db, db.begin():
            directory_dispatch.enqueue_directory_step(
                db, db.get(AdDirectoryRun, regular), initial=True
            )
            assert [r.id for r in sent(db)] == [regular]


def test_missing_service_serialized_task_uses_existing_metadata_strip(
    committed_sync_seed, monkeypatch, redis_client
):
    from tests.modules.reporting.test_directory_service_share import (
        test_serialized_old_or_marked_task_reads_without_private_metadata,
    )

    test_serialized_old_or_marked_task_reads_without_private_metadata(
        committed_sync_seed, monkeypatch, redis_client, "status_missing"
    )


def test_scanner_page_and_local_publish_recovery_preserve_turn(
    session, sync_seed, monkeypatch
):
    from app.modules.ads.sync_models import AdDirectoryPage
    from app.modules.reporting.store_models import DirectoryDispatchCursor

    now = datetime.now(UTC)
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_BATCH", 1)
    session.add(
        DirectoryDispatchCursor(
            tenant_id=sync_seed.tenant_id,
            last_dispatched_at=now,
            last_status_missing=False,
        )
    )
    for terminal in (False, True):
        run = pending(session, sync_seed, "status_missing", now)
        run.next_page = 1 if terminal else 2
        if terminal:
            session.add(
                AdDirectoryPage(
                    run_id=run.id,
                    page=1,
                    tenant_id=run.tenant_id,
                    advertiser_id=run.advertiser_id,
                    claim_generation=1,
                    complete=True,
                    items=[],
                    materials=[],
                    evidence={},
                )
            )
        session.flush()
        assert job_tasks._dispatch_directories(session, now) == 1
        session.expire_all()
        assert (
            session.get(
                DirectoryDispatchCursor, sync_seed.tenant_id
            ).last_status_missing
            is False
        )
        run.status = "COMPLETE"
        session.flush()


def test_initial_flag_cannot_advance_recovery_turn(session, sync_seed):
    from app.modules.reporting.store_models import DirectoryDispatchCursor

    now = datetime.now(UTC)
    session.add(
        DirectoryDispatchCursor(
            tenant_id=sync_seed.tenant_id,
            last_dispatched_at=now,
            last_status_missing=False,
        )
    )
    for publish_only in (False, True):
        run = pending(session, sync_seed, "status_missing", now)
        run.next_page = 1 if publish_only else 2
        session.flush()
        directory_dispatch.enqueue_directory_step(
            session, run, initial=True, publish_only=publish_only
        )
        session.expire_all()
        assert (
            session.get(
                DirectoryDispatchCursor, sync_seed.tenant_id
            ).last_status_missing
            is False
        )
        run.status = "COMPLETE"
        session.flush()
