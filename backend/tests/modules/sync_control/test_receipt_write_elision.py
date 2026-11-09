"""已提交收据不重复争锁；失败与发送期间新增的成本仍须可靠重试。"""

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event

from app.core.db import engine
from app.modules.sync_control.costs import _LedgerObserver
from app.modules.sync_control.models import SyncCostLedger
from tests.modules.sync_control.test_actual_cost import reserve


def test_acknowledged_receipt_does_not_relock_for_unchanged_flush(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        ledger, _ = reserve(db, seed)
        db.flush()
        db.expunge(ledger)
    observer = _LedgerObserver(engine, ledger)
    observer.flush(required=True)
    with factory() as locker, locker.begin():
        locker.get(SyncCostLedger, ledger.id, with_for_update=True)
        # 原实现会在另一连接上等待500ms并抛错；无变化不应再访问账本。
        observer.flush(required=True)
    observer.attempt(ledger.physical_endpoint)
    observer.finished = True
    observer.flush(required=True)
    with factory() as db:
        row = db.get(SyncCostLedger, ledger.id)
        assert row.closed and row.observed_units == 1


def test_new_endpoint_flush_does_not_relock_acknowledged_primary(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        ledger, _ = reserve(db, seed)
        db.flush()
        db.expunge(ledger)
    observer = _LedgerObserver(engine, ledger)
    observer.flush(required=True)
    with factory() as locker, locker.begin():
        locker.get(SyncCostLedger, ledger.id, with_for_update=True)
        observer.prepare("GET /new-endpoint/")
    observer.finished = True
    observer.flush(required=True)


def test_failed_close_is_retried_and_acknowledged_close_is_not_rewritten(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        ledger, _ = reserve(db, seed)
        db.flush()
        db.expunge(ledger)
    observer = _LedgerObserver(engine, ledger)
    observer.flush(required=True)
    observer.finished = True

    def fail(_connection):
        raise RuntimeError("receipt commit rejected")

    event.listen(engine, "commit", fail)
    try:
        observer.flush()
    finally:
        event.remove(engine, "commit", fail)
    with factory() as db:
        assert not db.get(SyncCostLedger, ledger.id).closed
    observer.flush(required=True)
    with factory() as db:
        version = db.get(SyncCostLedger, ledger.id).receipt_version
        assert db.get(SyncCostLedger, ledger.id).closed
    with factory() as locker, locker.begin():
        locker.get(SyncCostLedger, ledger.id, with_for_update=True)
        observer.flush(required=True)
    with factory() as db:
        assert db.get(SyncCostLedger, ledger.id).receipt_version == version


def test_attempt_during_receipt_commit_is_not_acknowledged_until_next_flush(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    with factory() as db, db.begin():
        ledger, _ = reserve(db, seed)
        db.flush()
        db.expunge(ledger)
    observer = _LedgerObserver(engine, ledger)

    def send_during_commit(_connection):
        with ThreadPoolExecutor(max_workers=1) as sdk:
            sdk.submit(observer.attempt, ledger.physical_endpoint).result(timeout=2)

    event.listen(engine, "commit", send_during_commit)
    try:
        observer.flush(required=True)
    finally:
        event.remove(engine, "commit", send_during_commit)
    with factory() as db:
        assert db.get(SyncCostLedger, ledger.id).observed_units == 0
    observer.finished = True
    observer.flush(required=True)
    with factory() as db:
        row = db.get(SyncCostLedger, ledger.id)
        assert row.observed_units == 1 and row.closed
