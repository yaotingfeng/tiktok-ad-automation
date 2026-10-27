"""来源核查只恢复丢失的当前消息，不能重放同代历史造成重复风暴。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.materials.distribution import repair_material_dispatches
from tests.modules.materials.test_source_uploads import seed_operation
from tests.modules.materials.test_source_uploads import source_env as source_env


def _message(env, operation, *, published, due):
    return PendingDispatch(
        tenant_id=env["context"].tenant_id,
        actor_id=env["context"].actor_id,
        task_name="materials.verify_original",
        task_key=f"source-repair:{uuid4()}",
        payload={
            "operation_id": str(operation),
            "material_id": str(env["material_id"]),
            "revision": 2,
        },
        published_at=published,
        available_at=due,
    )


@pytest.mark.parametrize("scheduled_revision, repaired", [(2, 0), (1, 1)])
def test_scheduled_current_source_prevents_rearming_old_duplicate(
    source_env, scheduled_revision, repaired
):
    operation = seed_operation(
        source_env, status="result_unknown", evidence={"revision": 2}
    )
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        old = _message(
            source_env,
            operation,
            published=now - timedelta(minutes=5),
            due=now - timedelta(minutes=5),
        )
        scheduled = _message(
            source_env, operation, published=None, due=now + timedelta(minutes=1)
        )
        scheduled.payload = {**scheduled.payload, "revision": scheduled_revision}
        db.add_all([old, scheduled])
        db.flush()
        assert repair_material_dispatches(db) == repaired
        db.refresh(old)
        assert (old.published_at is None) == bool(repaired)


def test_source_repair_rearms_only_one_current_message_per_operation(source_env):
    operation = seed_operation(
        source_env, status="result_unknown", evidence={"revision": 2}
    )
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        rows = [
            _message(
                source_env,
                operation,
                published=now - timedelta(minutes=5),
                due=now - timedelta(minutes=5),
            )
            for _ in range(4)
        ]
        obsolete = _message(
            source_env,
            operation,
            published=now - timedelta(minutes=5),
            due=now - timedelta(minutes=5),
        )
        obsolete.payload = {**obsolete.payload, "revision": 1}
        db.add_all([*rows, obsolete])
        db.flush()
        assert repair_material_dispatches(db) == 1
        for row in [*rows, obsolete]:
            db.refresh(row)
        assert sum(row.published_at is None for row in rows) == 1
        assert obsolete.published_at is not None
        assert repair_material_dispatches(db) == 0


def test_source_repair_waits_for_claim_expiry_and_keeps_original_claim(source_env):
    from app.modules.materials.models import MaterialAssetOperation

    operation = seed_operation(
        source_env, status="result_unknown", evidence={"revision": 2}
    )
    now, claim = datetime.now(UTC), uuid4()
    with Session(engine) as db, db.begin():
        owned = db.get(MaterialAssetOperation, operation)
        owned.attempt_token = claim
        owned.claimed_until = now + timedelta(minutes=1)
        message = _message(
            source_env,
            operation,
            published=now - timedelta(minutes=5),
            due=now - timedelta(minutes=5),
        )
        message.payload = {
            "material_id": str(source_env["material_id"]),
            "operation_id": str(operation),
            "claim_id": str(claim),
        }
        db.add(message)
        db.flush()
        assert repair_material_dispatches(db) == 0
        owned.claimed_until = now - timedelta(minutes=1)
        db.flush()
        assert repair_material_dispatches(db) == 1
        db.refresh(message)
        assert message.payload["claim_id"] == str(claim)
        assert message.published_at is None
