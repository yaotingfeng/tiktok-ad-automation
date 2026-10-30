"""目标视频推送落定即准备封面；不依赖任何广告提交。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial, MaterialAssetOperation
from app.modules.materials.source_cover_service import (
    prepare_source_cover,
    repair_source_cover_starts,
)
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_native_distribution import native_env as native_env
from tests.modules.materials.test_native_distribution import search_info, source_info
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def ready_target(env, redis_client, wire):
    prepared = queue(env, env["target"])
    wire[1].extend([source_info(), {}])
    run(env, redis_client, prepared.task_id, kind="prepare")
    wire[1].extend([{"list": []}, search_info()])
    run(env, redis_client, prepared.task_id)
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "ready"
    return dist, op, mapping


def event_for(db, op):
    event = db.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == op.tenant_id,
            PendingDispatch.task_key == f"target-cover:{op.id}",
        )
    ).one_or_none()
    assert event is not None, "视频推送成功后应独立登记目标封面准备事件"
    return event


def consume(db, env, op, event):
    prepare_source_cover(
        db,
        context=env["context"],
        operation_id=op.id,
        dispatch_id=event.id,
        purpose="BUILD",
    )


def jobs(db, env):
    return db.exec(
        select(MaterialCoverJob).where(
            MaterialCoverJob.tenant_id == env["context"].tenant_id,
            MaterialCoverJob.advertiser_id == env["target"],
        )
    ).all()


def test_target_ready_creates_one_cover_without_ad_submission(
    native_env, redis_client, wire
):
    dist, op, mapping = ready_target(native_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        event = event_for(db, op)
        for _ in range(2):
            consume(db, native_env, op, event)
        covers = jobs(db, native_env)
        assert len(covers) == 1
        assert covers[0].purpose == "BUILD"
        assert covers[0].video_id == mapping.video_id
        assert covers[0].request_armed_at is None
        assert covers[0].advertiser_id == native_env["target"]
        covers[0].status, covers[0].error_code = "BLOCKED", "cover_unavailable"
    assert state(dist.id)[0].status == "ready"
    assert len(wire[0]) == 4


def test_lost_target_start_republishes_then_stops_after_ack(
    native_env, redis_client, wire
):
    _, op, _ = ready_target(native_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        event = event_for(db, op)
        event.published_at = datetime.now(UTC) - timedelta(minutes=2)
        db.flush()
        assert repair_source_cover_starts(db) == 1
        consume(db, native_env, op, event)
        event.published_at = datetime.now(UTC) - timedelta(minutes=2)
        db.flush()
        assert repair_source_cover_starts(db) == 0


def test_target_start_ack_rolls_back_with_job(native_env, redis_client, wire):
    _, op, _ = ready_target(native_env, redis_client, wire)
    with pytest.raises(RuntimeError):
        with Session(engine) as db, db.begin():
            consume(db, native_env, op, event_for(db, op))
            raise RuntimeError("commit interrupted")
    with Session(engine) as db, db.begin():
        assert not jobs(db, native_env)
        saved = db.get(MaterialAssetOperation, op.id)
        assert saved is not None
        assert "target_cover_dispatch_id" not in saved.remote_response
        consume(db, native_env, op, event_for(db, op))
        assert len(jobs(db, native_env)) == 1


@pytest.mark.parametrize("change", ["mapping", "receipt"])
def test_delayed_target_event_cannot_prepare_replaced_mapping(
    native_env, redis_client, wire, change
):
    _, op, mapping = ready_target(native_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        actual = db.get(AccountMaterial, mapping.id)
        assert actual is not None
        if change == "mapping":
            actual.video_id = "replacement-video"
        else:
            # 已知回执与当前映射不一致时，不能用事件替换真实视频身份。
            op_row = db.get(MaterialAssetOperation, op.id)
            assert op_row is not None
            op_row.remote_response = {
                **op_row.remote_response,
                "video_id": "previous-video",
            }
        db.flush()
        consume(db, native_env, op, event_for(db, op))
        assert not jobs(db, native_env)


def test_target_event_rejects_wrong_tenant_actor_and_purpose(
    native_env, redis_client, wire
):
    _, op, _ = ready_target(native_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        event = event_for(db, op)
        for context in [
            TenantContext(
                tenant_id=uuid4(),
                actor_id=native_env["context"].actor_id,
                role="operator",
            ),
            TenantContext(
                tenant_id=native_env["context"].tenant_id,
                actor_id=uuid4(),
                role="operator",
            ),
        ]:
            with pytest.raises(DomainError):
                prepare_source_cover(
                    db,
                    context=context,
                    operation_id=op.id,
                    dispatch_id=event.id,
                    purpose="BUILD",
                )
        with pytest.raises(DomainError):
            prepare_source_cover(
                db,
                context=native_env["context"],
                operation_id=op.id,
                dispatch_id=event.id,
            )
        assert not jobs(db, native_env)


def test_verified_target_image_is_reused_without_new_cover(
    native_env, redis_client, wire
):
    _, op, mapping = ready_target(native_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        actual = db.get(AccountMaterial, mapping.id)
        assert actual is not None
        actual.image_id = "existing-target-image"
        db.flush()
        consume(db, native_env, op, event_for(db, op))
        assert not jobs(db, native_env)
        assert actual.image_id == "existing-target-image"
    assert len(wire[0]) == 4


def test_revoked_target_upload_cannot_prepare_cover(native_env, redis_client, wire):
    from app.modules.accounts.models import BCAccountAccess

    _, op, _ = ready_target(native_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        grant = db.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == op.tenant_id,
                BCAccountAccess.advertiser_id == native_env["target"],
            )
        ).one()
        grant.can_upload = False
        db.flush()
        with pytest.raises(DomainError):
            consume(db, native_env, op, event_for(db, op))
        assert not jobs(db, native_env)
        saved = db.get(MaterialAssetOperation, op.id)
        assert saved is not None
        assert "target_cover_dispatch_id" not in saved.remote_response


def test_unverified_target_mapping_keeps_start_pending_until_verified(
    native_env, redis_client, wire
):
    _, op, mapping = ready_target(native_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        actual = db.get(AccountMaterial, mapping.id)
        assert actual is not None
        actual.status = "result_unknown"
        db.flush()
        consume(db, native_env, op, event_for(db, op))
        assert not jobs(db, native_env)
        saved = db.get(MaterialAssetOperation, op.id)
        assert saved is not None
        assert "target_cover_dispatch_id" not in saved.remote_response
        actual.status = "available"
        db.flush()
        consume(db, native_env, op, event_for(db, op))
        assert len(jobs(db, native_env)) == 1


def test_worker_consumes_persisted_target_payload(
    native_env, redis_client, wire, monkeypatch
):
    from types import SimpleNamespace

    from app.modules.materials.source_cover_tasks import prepare_source_cover_task

    _, op, _ = ready_target(native_env, redis_client, wire)
    with Session(engine) as db:
        event = event_for(db, op)
        identity, payload = event.id, dict(event.payload)
    # 只替身进程身份边界；实际 Celery 参数解析、服务与持久化均执行。
    monkeypatch.setattr(
        "app.modules.materials.tasks.current_process",
        lambda: SimpleNamespace(daemon=True, name="ForkPoolWorker-test"),
    )
    task = prepare_source_cover_task
    task.push_request(
        id=str(identity), timelimit=(45, 40), called_directly=False, is_eager=False
    )
    try:
        task.run(
            tenant_id=str(native_env["context"].tenant_id),
            actor_id=str(native_env["context"].actor_id),
            payload=payload,
        )
    finally:
        task.pop_request()
    with Session(engine) as db:
        assert len(jobs(db, native_env)) == 1
        assert jobs(db, native_env)[0].purpose == "BUILD"
