"""真实万级候选验证分页先隔离范围，合法锚点不会退化成单条共享。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, func, text
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.materials.batch_distribution import _identity
from app.modules.materials.batch_models import (
    MaterialShareBatch,
    MaterialShareBatchMember,
)
from app.modules.materials.distribution import repair_material_dispatches
from app.modules.materials.models import (
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
)
from tests.integrations.tiktok.gateway_support import business_calls
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.materials.test_batch_distribution import (
    database_engine as database_engine,
)
from tests.modules.materials.test_batch_distribution import prepare_wire, seed_rectangle
from tests.modules.materials.test_batch_distribution import (
    retain_build_history as retain_build_history,
)
from tests.modules.materials.test_batch_distribution import share_case as share_case
from tests.modules.materials.test_distribution import run, state
from tests.modules.materials.test_readiness import target
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


def seed_preceding_rows(database_engine, env, anchor_id, *, eligible):
    """仅批量建测试库存；真实共享、权限、领取及传输均使用生产入口。"""
    with Session(database_engine) as db, db.begin():
        anchor = db.get(MaterialDistribution, anchor_id)
        operation = db.get(MaterialAssetOperation, anchor.operation_id)
        material = db.get(MaterialFile, anchor.material_id)
        other_target = target(db, env, advertiser_id="90071992547999999")
        originals = material.model_dump(), operation.model_dump(), anchor.model_dump()
        materials, operations, distributions = [], [], []
        for index in range(10001):
            # 小 UUID 确保全部排在真实锚点之前，不依赖随机排序碰巧触发问题。
            material_id, operation_id, distribution_id = (
                UUID(int=index + 1),
                uuid4(),
                uuid4(),
            )
            assert material_id < anchor.material_id
            file_values = originals[0] | {
                "id": material_id,
                "object_key": f"synthetic-candidate-{index}",
            }
            op_values = originals[1] | {
                "id": operation_id,
                "material_id": material_id,
                "advertiser_id": other_target,
                "remote_response": originals[1]["remote_response"]
                | {"source_video_id": f"unrelated-video-{index}"},
            }
            dist_values = originals[2] | {
                "id": distribution_id,
                "material_id": material_id,
                "advertiser_id": other_target,
                "operation_id": operation_id,
            }
            if not eligible:
                reason = index % 7
                if reason == 0:
                    dist_values["target_route"] = anchor.target_route | {
                        "authorization_revision": 2
                    }
                    op_values["frozen_route"] = dist_values["target_route"]
                elif reason == 1:
                    dist_values["source_route"] = anchor.source_route | {
                        "authorization_revision": 2
                    }
                elif reason == 2:
                    op_values["remote_response"]["source_advertiser_id"] = other_target
                elif reason == 3:
                    op_values["remote_response"]["transport"] = "url_relay"
                elif reason == 4:
                    op_values["remote_response"]["send_armed"] = True
                elif reason == 5:
                    op_values["attempt_token"] = uuid4()
                    op_values["claimed_until"] = datetime.now(UTC) + timedelta(hours=1)
                else:
                    op_values["remote_response"]["source_video_id"] = index
            materials.append(file_values)
            operations.append(op_values)
            distributions.append(dist_values)
        # 使用真实外键/约束批量落测试数据，避免一万次业务建数掩盖分页性能。
        connection = db.connection()
        connection.execute(MaterialFile.__table__.insert(), materials)
        connection.execute(MaterialAssetOperation.__table__.insert(), operations)
        connection.execute(MaterialDistribution.__table__.insert(), distributions)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "eligible_decoys",
    [False, True],
    ids=["excluded-before-limit", "anchor-outside-page"],
)
def test_ten_thousand_preceding_rows_cannot_split_legal_anchor_rectangle(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    gateway_case,
    eligible_decoys,
    record_property,
):
    # 能捕获两类退化：无关范围在分页后过滤，以及同范围的大库遗漏锚点。
    tasks, videos = seed_rectangle(share_case, database_engine, materials=1, targets=2)
    seed_preceding_rows(database_engine, share_case, tasks[0], eligible=eligible_decoys)
    # 过期且明确未发送的领取仍可接续，不能把所有有 claimed_until 的成员排除。
    with Session(database_engine) as db, db.begin():
        peer = db.get(MaterialDistribution, tasks[1])
        operation = db.get(MaterialAssetOperation, peer.operation_id)
        operation.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        operation.remote_response = operation.remote_response | {"send_armed": False}
    prepare_wire(gateway_wire, videos)
    candidates = []

    def counted(connection, cursor, statement, _parameters, _context, _many):
        if (
            connection.engine.url.database == database_engine.url.database
            and "batch_rectangle_candidates" in statement
        ):
            candidates.append(cursor.rowcount)

    event.listen(Engine, "after_cursor_execute", counted)
    started = perf_counter()
    try:
        run(share_case, redis_client, tasks[0], kind="prepare")
    finally:
        elapsed = perf_counter() - started
        event.remove(Engine, "after_cursor_execute", counted)
    record_property("candidate_rows", candidates)
    record_property("candidate_worker_seconds", elapsed)
    assert [state(task)[0].status for task in tasks] == ["verifying", "verifying"]
    with Session(database_engine) as db:
        members = db.exec(select(MaterialShareBatchMember)).all()
        assert {member.distribution_id for member in members} == set(tasks)
        assert len({member.batch_id for member in members}) == 1
        assert (
            db.exec(
                select(func.count())
                .select_from(MaterialDistribution)
                .where(
                    col(MaterialDistribution.id).not_in(tasks),
                    MaterialDistribution.status == "queued",
                )
            ).one()
            == 10001
        )
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 2
    assert candidates == [2], candidates


def test_contended_batch_claim_exits_without_send_and_original_dispatch_recovers(
    share_case, database_engine, gateway_wire, redis_client, gateway_case
):
    # 能捕获领取锁等待没有期限，或锁超时落入单条发送/伪造UNKNOWN的错误。
    tasks, videos = seed_rectangle(share_case, database_engine, materials=1, targets=2)
    prepare_wire(gateway_wire, videos)
    with Session(database_engine) as db, db.begin():
        anchor = db.get(MaterialDistribution, tasks[0])
        op = db.get(MaterialAssetOperation, anchor.operation_id)
        lock_key = int.from_bytes(
            sha256(repr(_identity(anchor, op)).encode()).digest()[:8],
            "big",
            signed=True,
        )
        original_operation = op.model_dump()
        messages = db.exec(select(PendingDispatch)).all()
        assert len(messages) == 2
        for message in messages:
            message.published_at = datetime.now(UTC) - timedelta(minutes=5)
        original_messages = {message.id: message.model_dump() for message in messages}
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(database_engine) as held, held.begin():
            held.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
            result = pool.submit(
                run, share_case, redis_client, tasks[0], kind="prepare"
            )
            with pytest.raises(DomainError) as failure:
                result.result(timeout=7)
            assert failure.value.code == "tiktok_local_resources_unavailable"
            assert failure.value.retryable
            assert not business_calls(gateway_wire, gateway_case[1].channel)
            assert [state(task)[0].status for task in tasks] == ["queued", "queued"]
            assert state(tasks[0])[1].model_dump() == original_operation
            with Session(database_engine) as db:
                assert not db.exec(select(MaterialShareBatch)).all()
                assert {
                    row.id: row.model_dump()
                    for row in db.exec(select(PendingDispatch)).all()
                } == original_messages
    with Session(database_engine) as db, db.begin():
        assert repair_material_dispatches(db) == 2
    with Session(database_engine) as db:
        messages = db.exec(select(PendingDispatch)).all()
        assert {message.id for message in messages} == set(original_messages)
        for message in messages:
            assert message.published_at is None
            assert message.payload == original_messages[message.id]["payload"]
            assert message.task_key == original_messages[message.id]["task_key"]
    payload = next(
        message["payload"]
        for message in original_messages.values()
        if message["payload"]["distribution_id"] == str(tasks[0])
    )
    run(
        share_case,
        redis_client,
        tasks[0],
        kind="prepare",
        operation_id=UUID(payload["operation_id"]),
        revision=payload["revision"],
    )
    assert [state(task)[0].status for task in tasks] == ["verifying", "verifying"]
    with Session(database_engine) as db:
        assert len(db.exec(select(MaterialShareBatch)).all()) == 1
