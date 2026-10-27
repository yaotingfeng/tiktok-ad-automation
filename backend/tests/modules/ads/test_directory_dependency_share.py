"""背景定向依赖与旧全量各有保底；不改变总窗口或外部服务分类。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import job_tasks
from tests.modules.ads.test_directory_sync import _run


def _pending(session, seed, *, targeted, age):
    row = _run(session, seed, kind="ad", ad_type="SMART_PLUS")
    row.workload = "background"
    row.created_at = age
    row.query = row.query | {
        "ids": [str(row.id)] if targeted else [],
        "include_deleted": True,
    }
    return row


def _sent(session):
    messages = session.exec(
        select(PendingDispatch).where(PendingDispatch.task_name == "ads.sync_step")
    ).all()
    return [
        session.get(AdDirectoryRun, UUID(message.payload["run_id"]))
        for message in messages
    ]


def test_background_dependency_and_full_scans_each_get_four_slots(
    session, directory_seed
):
    now = datetime.now(UTC)
    full = [
        _pending(
            session,
            directory_seed,
            targeted=False,
            age=now - timedelta(hours=5, seconds=i),
        )
        for i in range(2000)
    ]
    dependencies = [
        _pending(
            session,
            directory_seed,
            targeted=True,
            age=now - timedelta(minutes=10, seconds=i),
        )
        for i in range(8)
    ]
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 8
    sent = _sent(session)
    assert sum(bool(row.query["ids"]) for row in sent) == 4
    assert {row.id for row in sent if row.query["ids"]} == {
        row.id for row in dependencies[-4:]
    }
    assert {row.id for row in sent if not row.query["ids"]} == {
        row.id for row in full[-4:]
    }
    assert all(row.workload == "background" for row in sent)
    assert job_tasks._dispatch_directories(session, now) == 0


@pytest.mark.parametrize("occupied_targeted", [True, False])
def test_background_share_accounts_for_existing_claims(
    session, directory_seed, occupied_targeted
):
    now = datetime.now(UTC)
    for _ in range(7):
        row = _pending(
            session,
            directory_seed,
            targeted=occupied_targeted,
            age=now - timedelta(days=1),
        )
        row.status, row.claimed_until = "RUNNING", now + timedelta(minutes=1)
    _pending(
        session,
        directory_seed,
        targeted=occupied_targeted,
        age=now - timedelta(hours=5),
    )
    desired = _pending(
        session,
        directory_seed,
        targeted=not occupied_targeted,
        age=now - timedelta(minutes=1),
    )
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 1
    assert [row.id for row in _sent(session)] == [desired.id]


@pytest.mark.parametrize("targeted", [True, False])
def test_background_unused_share_is_borrowed(session, directory_seed, targeted):
    now = datetime.now(UTC)
    for _ in range(9):
        _pending(
            session, directory_seed, targeted=targeted, age=now - timedelta(hours=1)
        )
    session.flush()
    assert job_tasks._dispatch_directories(session, now) == 8
    assert len(_sent(session)) == 8
    assert job_tasks._dispatch_directories(session, now) == 0
