"""真实 PostgreSQL/Redis 验证调用前资源等待不会拖过任务期限。"""

from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from traceback import format_exception
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_redis, bounded_session


def deadline(seconds=3):
    return datetime.now(UTC) + timedelta(seconds=seconds)


def test_database_callback_owns_connection_and_rolls_back():
    marker = str(uuid4())
    with engine.connect() as outer:
        outer_pid = outer.execute(text("SELECT pg_backend_pid()")).scalar_one()
        with bounded_session(engine, task_deadline=deadline()) as session:
            assert (
                session.execute(text("SELECT pg_backend_pid()")).scalar_one()
                != outer_pid
            )
            session.execute(
                text(
                    "INSERT INTO tenant (id,name,active) VALUES (:id,'bounded-test',true)"
                ),
                {"id": marker},
            )
        assert (
            outer.execute(
                text("SELECT count(*) FROM tenant WHERE id=:id"), {"id": marker}
            ).scalar_one()
            == 0
        )


def test_database_statement_is_bounded_and_error_is_sanitized():
    started = monotonic()
    with pytest.raises(DomainError) as failure:
        with bounded_session(engine, task_deadline=deadline()) as session:
            session.execute(text("SELECT pg_sleep(8) /* synthetic-private-marker */"))
    assert monotonic() - started < 5
    assert failure.value.code == "tiktok_local_resources_unavailable"
    assert "synthetic-private-marker" not in str(failure.value)
    assert failure.value.__suppress_context__
    assert "synthetic-private-marker" not in "".join(format_exception(failure.value))


def test_expired_callback_budget_never_enters_database_or_redis(redis_client):
    for resource in (
        bounded_session(engine, task_deadline=deadline(-1)),
        bounded_redis(redis_client, task_deadline=deadline(-1)),
    ):
        with pytest.raises(DomainError) as failure:
            with resource:
                pytest.fail("Expired callbacks must not access remote resources")
        assert failure.value.code == "tiktok_call_deadline_exceeded"


def test_redis_wait_is_bounded_without_mutating_original_pool(redis_client):
    original = dict(redis_client.connection_pool.connection_kwargs)
    key = f"bounded-resource:{uuid4()}"
    started = monotonic()
    with pytest.raises(DomainError) as failure:
        with bounded_redis(redis_client, task_deadline=deadline(0.15)) as client:
            client.blpop(key, timeout=8)
    assert monotonic() - started < 2
    assert failure.value.code == "tiktok_local_resources_unavailable"
    assert failure.value.__context__ is None
    assert redis_client.connection_pool.connection_kwargs == original
    assert redis_client.ping()


def test_redis_rechecks_deadline_for_cleanup_after_outbound_work(redis_client):
    expires = deadline(1)
    with bounded_redis(redis_client, task_deadline=expires) as client:
        assert client.ping()
        # 模拟远端工作耗尽期限，清理阶段不能再次取得完整 socket 预算。
        sleep(1.05)
        with pytest.raises(DomainError) as failure:
            client.ping()
    assert failure.value.code == "tiktok_call_deadline_exceeded"
