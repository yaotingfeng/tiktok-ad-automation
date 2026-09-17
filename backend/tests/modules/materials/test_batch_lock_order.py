"""真实 PG：跨共享批次核验不能按素材顺序反向持有批次锁。"""

from threading import Thread
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.modules.materials.batch_models import (
    MaterialShareBatch,
    MaterialShareBatchMember,
)
from app.modules.materials.models import MaterialAssetOperation, MaterialDistribution
from tests.modules.materials.test_batch_distribution import (
    app_config as app_config,
)
from tests.modules.materials.test_batch_distribution import (
    database_engine as database_engine,
)
from tests.modules.materials.test_batch_distribution import (
    gateway_case as gateway_case,
)
from tests.modules.materials.test_batch_distribution import (
    gateway_wire as gateway_wire,
)
from tests.modules.materials.test_batch_distribution import (
    isolated_strategy_database as isolated_strategy_database,
)
from tests.modules.materials.test_batch_distribution import (
    policy as policy,
)
from tests.modules.materials.test_batch_distribution import (
    retain_build_history as retain_build_history,
)
from tests.modules.materials.test_batch_distribution import (
    seed_rectangle,
    selective_sdk_wire,
)
from tests.modules.materials.test_batch_distribution import (
    share_case as share_case,
)
from tests.modules.materials.test_distribution import run, state


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_cross_batch_verification_locks_batch_ids_before_updating_members(
    share_case, database_engine, gateway_wire, redis_client, monkeypatch
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=1)
    with Session(database_engine) as db, db.begin():
        ordered = sorted(
            tasks, key=lambda task: db.get(MaterialDistribution, task).material_id
        )
        for index, task in enumerate(ordered):
            dist = db.get(MaterialDistribution, task)
            op = db.get(MaterialAssetOperation, dist.operation_id)
            batch_id = UUID(int=2 - index)
            db.add(
                MaterialShareBatch(
                    id=batch_id,
                    tenant_id=dist.tenant_id,
                    bc_id=dist.bc_id,
                    actor_id=dist.actor_id,
                    source_advertiser_id=share_case["source"],
                    target_route=dist.target_route,
                    source_route=dist.source_route or dist.target_route,
                    request_digest="a" * 64,
                    claim_id=uuid4(),
                    status="acknowledged",
                    advertiser_ids=[dist.advertiser_id],
                )
            )
            db.flush()
            db.add(
                MaterialShareBatchMember(
                    tenant_id=dist.tenant_id,
                    bc_id=dist.bc_id,
                    batch_id=batch_id,
                    material_id=dist.material_id,
                    advertiser_id=dist.advertiser_id,
                    distribution_id=dist.id,
                    operation_id=op.id,
                    operation_claim=uuid4(),
                    operation_digest=op.request_digest,
                    source_video_id=rows[tasks.index(task)]["video_id"],
                    source_evidence={},
                    revision=0,
                    status="verifying",
                )
            )
            dist.status = op.status = "verifying"
            op.remote_response = {
                **op.remote_response,
                "share_batch_id": str(batch_id),
                "video_id": rows[tasks.index(task)]["video_id"],
            }
    selective_sdk_wire(monkeypatch, gateway_wire, rows)
    failures = []

    def verify():
        try:
            run(share_case, redis_client, tasks[0])
        except Exception as error:
            failures.append(error)

    contender = Thread(target=verify)
    waiting = high_locked = False
    try:
        with Session(database_engine) as holder, holder.begin():
            holder.exec(
                select(MaterialShareBatch)
                .where(MaterialShareBatch.id == UUID(int=1))
                .with_for_update()
            ).one()
            holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()
            contender.start()
            deadline = monotonic() + 15
            while monotonic() < deadline:
                with database_engine.connect() as check:
                    waiting = bool(
                        check.execute(
                            text(
                                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))"
                            ),
                            {"pid": holder_pid},
                        ).scalar_one()
                    )
                if waiting or failures:
                    break
                sleep(0.01)
            with Session(database_engine) as check:
                try:
                    check.exec(
                        select(MaterialShareBatch)
                        .where(MaterialShareBatch.id == UUID(int=2))
                        .with_for_update(nowait=True)
                    ).one()
                except OperationalError as error:
                    assert error.orig.sqlstate == "55P03"
                    high_locked = True
                finally:
                    check.rollback()
    finally:
        contender.join(timeout=20)
    assert waiting, failures
    assert not contender.is_alive()
    assert not failures
    assert not high_locked, "先锁高批次再等低批次会与另一账户的核验形成锁环"
    assert [state(task)[0].status for task in tasks] == ["ready", "ready"]
    with Session(database_engine) as db:
        assert [
            batch.status for batch in db.exec(select(MaterialShareBatch)).all()
        ] == ["completed", "completed"]
    assert len(gateway_wire["sdk_calls"]) == 1


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_batch_finish_does_not_hold_batch_while_waiting_for_member_material(
    share_case, database_engine, gateway_wire, redis_client, monkeypatch
):
    from app.modules.materials.batch_distribution import _finish
    from app.modules.materials.models import MaterialFile

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=1)
    selective_sdk_wire(monkeypatch, gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    with Session(database_engine) as db, db.begin():
        batch = db.exec(select(MaterialShareBatch)).one()
        batch.status = "sending"
        batch_id = batch.id
        material_id = min(state(task)[0].material_id for task in tasks)
    failures, results = [], []

    def finish():
        try:
            results.append(
                _finish(
                    database_engine,
                    share_case["context"],
                    batch_id,
                    effect="ACKNOWLEDGED",
                    code=None,
                )
            )
        except Exception as error:
            failures.append(error)

    contender = Thread(target=finish)
    waiting = batch_locked = False
    try:
        with Session(database_engine) as holder, holder.begin():
            holder.exec(
                select(MaterialFile)
                .where(MaterialFile.id == material_id)
                .with_for_update()
            ).one()
            holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()
            contender.start()
            deadline = monotonic() + 10
            while monotonic() < deadline:
                with database_engine.connect() as check:
                    waiting = bool(
                        check.execute(
                            text(
                                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))"
                            ),
                            {"pid": holder_pid},
                        ).scalar_one()
                    )
                if waiting or failures:
                    break
                sleep(0.01)
            with Session(database_engine) as check:
                try:
                    check.exec(
                        select(MaterialShareBatch)
                        .where(MaterialShareBatch.id == batch_id)
                        .with_for_update(nowait=True)
                    ).one()
                except OperationalError as error:
                    assert error.orig.sqlstate == "55P03"
                    batch_locked = True
                finally:
                    check.rollback()
    finally:
        contender.join(timeout=20)
    assert waiting, failures
    assert not contender.is_alive()
    assert not failures
    assert not batch_locked, "收口先锁批次再等素材，会与先素材后批次的核验反序"
    assert results == [True]
