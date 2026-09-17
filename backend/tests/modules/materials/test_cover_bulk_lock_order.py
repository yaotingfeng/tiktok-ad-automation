"""真实批读事务须按ID锁封面，不能与广告刷新形成相反锁序。"""

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session

from app.modules.materials.cover_models import MaterialCoverJob
from tests.modules.materials.test_cover_bulk_verification import (
    app_config as app_config,
)
from tests.modules.materials.test_cover_bulk_verification import (
    cover_env as cover_env,
)
from tests.modules.materials.test_cover_bulk_verification import (
    database_engine as database_engine,
)
from tests.modules.materials.test_cover_bulk_verification import (
    gateway_case as gateway_case,
)
from tests.modules.materials.test_cover_bulk_verification import (
    gateway_wire as gateway_wire,
)
from tests.modules.materials.test_cover_bulk_verification import (
    job,
    known_jobs,
    replies,
    run,
)
from tests.modules.materials.test_cover_bulk_verification import (
    policy as policy,
)


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_every_bulk_transaction_locks_cover_ids_in_same_order_as_ad_refresh(
    cover_env, gateway_wire, database_engine, redis_client
):
    identities = known_jobs(cover_env, database_engine, 3)
    replies(cover_env, gateway_wire, database_engine, identities)
    transactions = {}

    def capture(connection, _cursor, statement, parameters, *_):
        if "FROM material_cover_job " not in statement or "FOR UPDATE" not in statement:
            return
        if not isinstance(parameters, dict):
            return
        locked = [value for value in parameters.values() if value in identities]
        if len(locked) != 1:
            return
        transaction = connection.get_transaction()
        sequence = transactions.setdefault(transaction, [])
        if locked[0] not in sequence:
            sequence.append(locked[0])

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        # 消费者首个消息不保证是最低ID；首次独立领取后，所有批事务须重排锁序。
        run(cover_env, database_engine, redis_client, max(identities), read=True)
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
    assert all(
        job(database_engine, identity).status == "READY" for identity in identities
    )
    groups = [sequence for sequence in transactions.values() if len(sequence) > 1]
    assert groups
    assert all(sequence == sorted(sequence) for sequence in groups), groups


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("previous_failures", [0, 2])
@pytest.mark.parametrize("known", [True, False])
def test_local_resource_read_failure_has_bounded_retry_without_new_upload(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
    monkeypatch,
    previous_failures,
    known,
):
    from redis import Redis
    from redis.exceptions import ConnectionError

    identities = known_jobs(cover_env, database_engine, 1)
    with Session(database_engine) as db, db.begin():
        current = db.get(MaterialCoverJob, identities[0])
        current.failure_count = previous_failures
        if not known:
            current.known_image_id = None

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("synthetic-private-redis-url")

    with monkeypatch.context() as patch:
        patch.setattr(Redis, "execute_command", unavailable)
        run(cover_env, database_engine, redis_client, identities[0], read=True)
    current = job(database_engine, identities[0])
    assert current.error_code == "tiktok_local_resources_unavailable"
    assert current.failure_count == previous_failures + 1
    assert bool(current.known_image_id) == known
    assert current.request_armed_at
    assert current.status == ("VERIFYING" if previous_failures == 0 else "UNKNOWN")
    assert bool(current.dispatch_id) == (previous_failures == 0)
    assert not gateway_wire["sdk_calls"]
    assert not [c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"]
