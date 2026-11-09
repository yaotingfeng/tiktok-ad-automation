"""真实PG：目标补派按当前精确消息定位，历史长度不放大堆读取。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.materials.distribution import (
    queue_distribution,
    repair_material_dispatches,
)
from app.modules.materials.models import (
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialUploadAttempt,
)
from tests.modules.materials.test_distribution import queue
from tests.modules.materials.test_readiness import asset, target
from tests.modules.materials.test_readiness import runtime_config as runtime_config
from tests.modules.materials.test_source_uploads import source_env as source_env


@pytest.fixture
def exact_case(source_env):
    with Session(engine) as db, db.begin():
        account = target(db, source_env)
        asset(db, source_env, account, status="result_unknown")
    return source_env, queue(source_env, account).task_id


def _current_message(db, case, mode="verify"):
    env, identity = case
    dist = db.get(MaterialDistribution, identity)
    operation = db.get(MaterialAssetOperation, dist.operation_id)
    operation.remote_response = {"revision": 7}
    operation.status, dist.status = "result_unknown", "verifying"
    claim = uuid4() if mode.startswith("claim") else None
    operation.attempt_token = claim
    operation.claimed_until = (
        datetime.now(UTC) - timedelta(minutes=1) if claim else None
    )
    observe, read_only = "observe" in mode, "read" in mode
    if observe and not read_only:
        # 只有来源拥有该操作才可唤醒观察消息；终态操作不走普通UNRESOLVED分支。
        operation.status = "succeeded"
        db.add(
            MaterialUploadAttempt(
                tenant_id=operation.tenant_id,
                bc_id=operation.bc_id,
                material_id=operation.material_id,
                advertiser_id=operation.advertiser_id,
                connection_id=env["connection_id"],
                operation_id=operation.id,
                status="succeeded",
                request_digest=operation.request_digest,
            )
        )
    before = set(
        db.exec(
            select(PendingDispatch.id).where(
                PendingDispatch.tenant_id == dist.tenant_id
            )
        ).all()
    )
    queue_distribution(
        db,
        dist,
        operation,
        kind="prepare" if mode == "prepare" else "verify",
        claim_id=claim,
        observe=observe,
        read_only=read_only,
    )
    db.flush()
    added = (
        set(
            db.exec(
                select(PendingDispatch.id).where(
                    PendingDispatch.tenant_id == dist.tenant_id
                )
            ).all()
        )
        - before
    )
    assert len(added) == 1
    message = db.get(PendingDispatch, added.pop())
    message.published_at = message.available_at = datetime.now(UTC) - timedelta(
        minutes=3
    )
    db.flush()
    return dist, operation, message


@pytest.mark.parametrize(
    "mode",
    ["prepare", "verify", "read", "claim", "claim_read", "observe", "observe_read"],
)
def test_current_message_forms_keep_exact_identity_and_payload(
    exact_case, mode, redis_client
):
    assert redis_client.ping()
    with Session(engine) as db, db.begin():
        dist, operation, message = _current_message(db, exact_case, mode)
        identity, key, payload, due = (
            message.id,
            message.task_key,
            dict(message.payload),
            message.available_at,
        )
        before = (
            dist.status,
            operation.status,
            operation.attempt_token,
            dict(operation.remote_response),
        )
        assert repair_material_dispatches(db) == 1
        db.refresh(message)
        assert message.published_at is None
        assert (
            message.id,
            message.task_key,
            message.payload,
            message.available_at,
        ) == (identity, key, payload, due)
        assert (
            dist.status,
            operation.status,
            operation.attempt_token,
            operation.remote_response,
        ) == before


@pytest.mark.parametrize(
    "change",
    [
        "future_due",
        "grace",
        "foreign_tenant",
        "wrong_distribution",
        "wrong_operation",
        "old_revision",
        "late_owner",
        "live_claim",
        "wrong_claim",
        "observe_without_source",
    ],
)
def test_ineligible_message_stays_published(exact_case, change):
    mode = (
        "claim"
        if change in {"live_claim", "wrong_claim"}
        else "observe"
        if change == "observe_without_source"
        else "verify"
    )
    with Session(engine) as db, db.begin():
        dist, operation, message = _current_message(db, exact_case, mode)
        now = datetime.now(UTC)
        if change == "future_due":
            message.available_at = now + timedelta(hours=1)
        elif change == "grace":
            message.published_at = now - timedelta(seconds=30)
        elif change == "foreign_tenant":
            message.tenant_id = uuid4()
        elif change == "wrong_distribution":
            message.payload = {**message.payload, "distribution_id": str(uuid4())}
        elif change == "wrong_operation":
            message.payload = {**message.payload, "operation_id": str(uuid4())}
        elif change == "old_revision":
            message.payload = {**message.payload, "revision": 6}
        elif change == "late_owner":
            replacement = MaterialAssetOperation(
                **{**operation.model_dump(), "id": uuid4()}
            )
            operation.status = "failed"
            db.flush()
            db.add(replacement)
            db.flush()
            dist.operation_id = replacement.id
        elif change == "live_claim":
            operation.claimed_until = now + timedelta(minutes=5)
        elif change == "wrong_claim":
            message.payload = {**message.payload, "claim_id": str(uuid4())}
        else:
            attempts = db.exec(
                select(MaterialUploadAttempt).where(
                    MaterialUploadAttempt.operation_id == operation.id
                )
            ).all()
            for attempt in attempts:
                db.delete(attempt)
        db.flush()
        published, due, payload = (
            message.published_at,
            message.available_at,
            dict(message.payload),
        )
        assert repair_material_dispatches(db) == 0
        db.refresh(message)
        assert (message.published_at, message.available_at, message.payload) == (
            published,
            due,
            payload,
        )
        if change == "foreign_tenant":
            db.delete(message)


def test_target_history_is_not_read_for_current_generation(exact_case):
    with Session(engine) as db, db.begin():
        dist, operation, message = _current_message(db, exact_case)
        db.execute(
            text("""
            INSERT INTO pending_dispatch(id,tenant_id,actor_id,task_name,task_key,payload,available_at,published_at,attempts)
            SELECT gen_random_uuid(),:tenant,:actor,'materials.verify_target',
              'old-target:'||gen_random_uuid()::text,
              jsonb_build_object('distribution_id',CAST(:distribution AS text),'operation_id',CAST(:operation AS text),'revision',1),
              now()-interval '1 day',now()-interval '1 day',0
            FROM generate_series(1,4000)
        """),
            {
                "tenant": dist.tenant_id,
                "actor": dist.actor_id,
                "distribution": str(dist.id),
                "operation": str(operation.id),
            },
        )
        db.flush()
        for table in (
            "pending_dispatch",
            "material_distribution",
            "material_asset_operation",
        ):
            db.execute(text(f"ANALYZE {table}"))
        captured = []

        def capture(_conn, _cursor, statement, parameters, *_args):
            if "repair_target_identities" in statement:
                captured.append((statement, parameters))

        conn = db.connection()
        event.listen(conn, "before_cursor_execute", capture)
        try:
            assert repair_material_dispatches(db) == 1
        finally:
            event.remove(conn, "before_cursor_execute", capture)
        assert message.published_at is None
        assert len(captured) == 1
        sql, parameters = captured[0]
        plan = conn.exec_driver_sql(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql, parameters
        ).scalar_one()[0]["Plan"]

        def inspected(node):
            rows = (
                (
                    (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0))
                    * node.get("Actual Loops", 0)
                )
                if node.get("Relation Name") == "pending_dispatch"
                else 0
            )
            return rows + sum(inspected(child) for child in node.get("Plans", []))

        # 真实查询可读当前消息及几个合法键，不可为当前代读取4000条旧历史。
        assert inspected(plan) < 32, inspected(plan)
