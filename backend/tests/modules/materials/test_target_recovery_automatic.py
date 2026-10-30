"""目标确认必须保留；本地争锁和已 ACK 共享的短暂不可见应自行恢复。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.materials import batch_verification, distribution
from app.modules.materials.models import MaterialAssetOperation, MaterialFile
from tests.modules.materials.test_distribution import run, state
from tests.modules.materials.test_readiness import runtime_config as runtime_config
from tests.modules.materials.test_reconciliation_budget import (
    unknown,
    unknown_peers,
    verified_response,
)
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


@pytest.mark.parametrize("batch", [False, True])
def test_read_preflight_does_not_wait_for_another_accounts_material_lock(
    source_env, redis_client, wire, monkeypatch, batch
):
    identities = unknown_peers(source_env, 2) if batch else [unknown(source_env)]
    original = distribution.open_tiktok_gateway

    @contextmanager
    def gateway_with_contending_material_lock(**kwargs):
        # 初始领取已经提交后，让另一事务持素材锁直至平台读取结束。
        # 外部请求仍经过真实 gateway/SDK，仅 HTTP 传输由 wire 替身。
        with Session(engine) as holder, holder.begin():
            holder.exec(
                select(MaterialFile)
                .where(MaterialFile.id == source_env["material_id"])
                .with_for_update()
            ).one()
            with original(**kwargs) as gateway:
                yield gateway

    monkeypatch.setattr(
        distribution, "open_tiktok_gateway", gateway_with_contending_material_lock
    )
    monkeypatch.setattr(
        batch_verification, "open_tiktok_gateway", gateway_with_contending_material_lock
    )
    response = verified_response()
    if batch:
        response["list"].append({**response["list"][0], "video_id": "known-peer-1"})
    wire[1].append(response)
    run(source_env, redis_client, identities[0], read_only=not batch)
    assert [state(identity)[0].status for identity in identities] == ["ready"] * len(
        identities
    )
    assert len(wire[0]) == 1 and wire[0][0][0] == "GET"


@pytest.mark.parametrize("batch", [False, True])
def test_local_resources_failure_does_not_spend_remote_reconciliation_budget(
    source_env, redis_client, wire, batch
):
    identities = unknown_peers(source_env, 2) if batch else [unknown(source_env)]
    for _ in range(4):
        wire[1].append(DomainError("tiktok_local_resources_unavailable", "local only"))
        run(source_env, redis_client, identities[0], read_only=not batch)
        # 模拟原消息到达下一次自动重试时间；不通过人工 reconcile 重开预算。
        with Session(engine) as db, db.begin():
            for identity in identities:
                current = db.get(MaterialAssetOperation, state(identity)[1].id)
                current.remote_response = {
                    **current.remote_response,
                    "reconciliation_retry_at": (
                        datetime.now(UTC) - timedelta(seconds=1)
                    ).isoformat(),
                }
    for identity in identities:
        dist, op, _ = state(identity)
        assert dist.status in {"queued", "verifying"}
        assert op.remote_response["reconciliation_budget"]["no_progress"] == 0
        assert op.remote_response["reconciliation_budget"]["claims"] == 0
        assert not op.remote_response.get("reconciliation_stopped")
        assert (
            op.remote_response["last_reconciliation_error"]
            == "tiktok_local_resources_unavailable"
        )

    response = verified_response()
    if batch:
        response["list"].append({**response["list"][0], "video_id": "known-peer-1"})
    wire[1].append(response)
    run(source_env, redis_client, identities[0], read_only=not batch)
    assert [state(identity)[0].status for identity in identities] == ["ready"] * len(
        identities
    )
    assert [call[0] for call in wire[0]] == ["GET"] * 5


def test_acknowledged_native_share_enters_cooldown_and_resumes_without_manual_reconcile(
    source_env, redis_client, wire
):
    identity = unknown(source_env)
    with Session(engine) as db, db.begin():
        op = db.get(MaterialAssetOperation, state(identity)[1].id)
        op.remote_response = {
            **op.remote_response,
            "transport": "native_share",
            "share_acknowledged": True,
        }
        original_digest, original_id = op.request_digest, op.id
    for _ in range(3):
        wire[1].append({"list": []})
        run(source_env, redis_client, identity)
    dist, op, mapping = state(identity)
    assert dist.status == op.status == "verifying"
    assert not op.remote_response.get("reconciliation_stopped")
    assert mapping is None
    due = datetime.fromisoformat(op.remote_response["reconciliation_retry_at"])
    assert due >= datetime.now(UTC) + timedelta(minutes=4)
    with Session(engine) as db:
        pending = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.payload["operation_id"].as_string() == str(op.id),
                PendingDispatch.payload["revision"].as_integer()
                == op.remote_response["revision"],
            )
        ).one()
        assert pending.task_name == "materials.verify_target"
        assert pending.available_at >= due
    # 提前到达的旧/重复消息不能跳过冷却；冷却后仍只有读取，没有重复写入。
    run(source_env, redis_client, identity)
    assert len(wire[0]) == 3
    with Session(engine) as db, db.begin():
        current = db.get(MaterialAssetOperation, op.id)
        current.remote_response = {
            **current.remote_response,
            "reconciliation_retry_at": (
                datetime.now(UTC) - timedelta(seconds=1)
            ).isoformat(),
        }
    wire[1].append(verified_response())
    run(source_env, redis_client, identity)
    dist, op, mapping = state(identity)
    assert dist.status == "ready" and mapping.video_id == "known-target"
    assert (op.id, op.request_digest) == (original_id, original_digest)
    assert [call[0] for call in wire[0]] == ["GET"] * 4


def test_cooling_member_is_not_pulled_forward_by_another_batch(
    source_env, redis_client, wire
):
    first, second = unknown_peers(source_env, 2)
    with Session(engine) as db, db.begin():
        op = db.get(MaterialAssetOperation, state(first)[1].id)
        op.remote_response = {
            **op.remote_response,
            "reconciliation_retry_at": (
                datetime.now(UTC) + timedelta(minutes=5)
            ).isoformat(),
        }
    response = verified_response()
    response["list"][0]["video_id"] = "known-peer-1"
    wire[1].append(response)
    run(source_env, redis_client, second)
    assert state(second)[0].status == "ready"
    assert state(first)[0].status == "result_unknown"
    assert state(first)[1].remote_response.get("reconciliation_budget") is None
    assert len(wire[0]) == 1


def test_changed_claim_during_http_is_not_published_by_old_batch(
    source_env, redis_client, wire
):
    first, second = unknown_peers(source_env, 2)
    original = state(first)[1]
    replacement_claim = uuid4()

    def response_after_replacement():
        with Session(engine) as db, db.begin():
            op = db.get(MaterialAssetOperation, original.id)
            op.attempt_token = replacement_claim
        response = verified_response()
        response["list"].append({**response["list"][0], "video_id": "known-peer-1"})
        return response

    wire[1].append(response_after_replacement)
    run(source_env, redis_client, first)
    assert state(first)[2] is None
    assert state(first)[1].attempt_token == replacement_claim
    assert state(second)[0].status == "ready"
