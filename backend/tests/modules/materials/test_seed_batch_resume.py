"""首次转存落定后先绑定同一内容全部目标，再交给唯一共享器。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import BCAccountAccess
from app.modules.materials.distribution import repair_material_dispatches
from app.modules.materials.models import MaterialAssetOperation, MaterialDistribution
from tests.modules.materials.test_bc_seeding import seed_env as seed_env
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import target
from tests.modules.materials.test_remote_only_distribution import info
from tests.modules.materials.test_remote_only_distribution import (
    remote_env as remote_env,
)
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.materials.test_url_ingest import url_env as url_env


def test_pending_seed_does_not_create_repeated_poll_generations(
    seed_env, redis_client, wire
):
    consumer = queue(seed_env, seed_env["target"]).task_id
    before = state(consumer)[1].remote_response.get("revision", 0)
    original = consumer_messages(seed_env, consumer)
    run(seed_env, redis_client, consumer, kind="prepare")
    run(seed_env, redis_client, consumer, kind="prepare")
    assert state(consumer)[1].remote_response.get("revision", 0) == before
    assert consumer_messages(seed_env, consumer) == original
    assert wire[0] == []


def test_ready_seed_binds_all_targets_before_any_share(seed_env, redis_client, wire):
    from app.modules.materials import bc_seeding

    with Session(engine) as db, db.begin():
        accounts = [seed_env["target"]] + [
            target(db, seed_env, advertiser_id=f"peer-{i}") for i in range(2)
        ]
    consumers = [queue(seed_env, account).task_id for account in accounts]
    with Session(engine) as db:
        owner = (
            db.exec(
                select(MaterialDistribution).where(
                    MaterialDistribution.advertiser_id == seed_env["primary"]
                )
            )
            .one()
            .id
        )
    wire[1].extend(
        [info(), [{"video_id": "primary-vid", "material_id": "primary-mid"}]]
    )
    run(seed_env, redis_client, owner, kind="prepare")
    if state(owner)[0].status != "ready":
        wire[1].append(info(vid="primary-vid", material_id="primary-mid"))
        run(seed_env, redis_client, owner)
    before = len(wire[0])
    assert (
        bc_seeding.resume_ready_seed_dependents(
            database_engine=engine,
            context=seed_env["context"],
            distribution_id=consumers[0],
        )
        == 3
    )
    with Session(engine) as db:
        for identity in consumers:
            dist = db.get(MaterialDistribution, identity)
            operation = db.get(MaterialAssetOperation, dist.operation_id)
            assert operation.remote_response["transport"] == "native_share"
            assert (
                operation.remote_response["source_advertiser_id"] == seed_env["primary"]
            )
    assert len(wire[0]) == before


def consumer_messages(env, consumer):
    with Session(engine) as db:
        return {
            row.id: row.model_dump()
            for row in db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == env["context"].tenant_id
                )
            ).all()
            if row.payload.get("distribution_id") == str(consumer)
        }


def seed_owner(env):
    with Session(engine) as db:
        return db.exec(
            select(MaterialDistribution.id).where(
                MaterialDistribution.tenant_id == env["context"].tenant_id,
                MaterialDistribution.advertiser_id == env["primary"],
            )
        ).one()


def mark_consumer_published(env, consumer):
    messages = consumer_messages(env, consumer)
    assert len(messages) == 1
    identity = next(iter(messages))
    with Session(engine) as db, db.begin():
        message = db.get(PendingDispatch, identity)
        message.available_at = message.published_at = datetime.now(UTC) - timedelta(
            minutes=5
        )
    return identity


def complete_seed(env, redis_client, wire):
    owner = seed_owner(env)
    wire[1].extend(
        [info(), [{"video_id": "primary-vid", "material_id": "primary-mid"}]]
    )
    run(env, redis_client, owner, kind="prepare")
    assert state(owner)[0].status == "ready"
    return owner


def test_seed_success_wakes_original_published_prepare_message(
    seed_env, redis_client, wire
):
    consumer = queue(seed_env, seed_env["target"]).task_id
    run(seed_env, redis_client, consumer, kind="prepare")
    identity = mark_consumer_published(seed_env, consumer)
    before = consumer_messages(seed_env, consumer)[identity]
    complete_seed(seed_env, redis_client, wire)
    messages = consumer_messages(seed_env, consumer)
    assert set(messages) == {identity}
    after = messages[identity]
    assert after["published_at"] is None
    assert after["available_at"] <= datetime.now(UTC)
    assert after["payload"] == before["payload"]
    assert after["task_key"] == before["task_key"]
    assert (
        state(consumer)[1].remote_response.get("revision", 0)
        == before["payload"]["revision"]
    )


def test_dispatch_repair_does_not_poll_consumer_while_seed_is_queued(
    seed_env, redis_client, wire
):
    consumer = queue(seed_env, seed_env["target"]).task_id
    run(seed_env, redis_client, consumer, kind="prepare")
    mark_consumer_published(seed_env, consumer)
    before = consumer_messages(seed_env, consumer)
    assert state(seed_owner(seed_env))[0].status == "queued"
    for _ in range(2):
        with Session(engine) as db, db.begin():
            assert repair_material_dispatches(db) == 0
    assert consumer_messages(seed_env, consumer) == before
    assert state(consumer)[1].remote_response.get("revision", 0) == 0
    assert wire[0] == []


@pytest.mark.parametrize("settled", ["ready", "blocked"])
def test_dispatch_repair_resumes_consumer_after_seed_settles(
    seed_env, redis_client, wire, settled
):
    consumer = queue(seed_env, seed_env["target"]).task_id
    run(seed_env, redis_client, consumer, kind="prepare")
    owner = seed_owner(seed_env)
    if settled == "ready":
        complete_seed(seed_env, redis_client, wire)
    else:
        # 正式权限失败落定 seed，不能用 SQL 伪造远端成功或发送结果。
        with Session(engine) as db, db.begin():
            db.get(
                BCAccountAccess,
                (
                    seed_env["context"].tenant_id,
                    seed_env["source_bc_id"],
                    "actual-account",
                    seed_env["connection_id"],
                ),
            ).authorized = False
        run(seed_env, redis_client, owner, kind="prepare")
    assert state(owner)[0].status == settled
    # 模拟唤醒消息已投递但 Worker 尚未接续；修复须复用原消息身份。
    identity = mark_consumer_published(seed_env, consumer)
    before = consumer_messages(seed_env, consumer)[identity]
    with Session(engine) as db, db.begin():
        assert repair_material_dispatches(db) == 1
    repaired = consumer_messages(seed_env, consumer)
    assert set(repaired) == {identity}
    assert repaired[identity]["published_at"] is None
    assert repaired[identity]["payload"] == before["payload"]
    if settled == "ready":
        wire[1].extend(
            [
                info(
                    vid="primary-vid",
                    material_id="primary-mid",
                    file_name="primary.mp4",
                ),
                {},
            ]
        )
    payload = before["payload"]
    run(
        seed_env,
        redis_client,
        consumer,
        kind="prepare",
        operation_id=UUID(payload["operation_id"]),
        revision=payload["revision"],
    )
    dist, operation, _ = state(consumer)
    if settled == "ready":
        assert (dist.status, operation.status) == ("verifying", "verifying"), (
            dist.reason_code,
            operation.remote_response,
        )
        assert operation.remote_response["transport"] == "native_share"
        assert operation.remote_response["source_advertiser_id"] == seed_env["primary"]
        assert len([call for call in wire[0] if "/video/ad/upload/" in call[1]]) == 1
    else:
        assert (dist.status, operation.status) == ("blocked", "failed")
        assert dist.reason_code == state(owner)[0].reason_code
        assert operation.remote_response["definite_no_effect"] is True
        assert wire[0] == []
