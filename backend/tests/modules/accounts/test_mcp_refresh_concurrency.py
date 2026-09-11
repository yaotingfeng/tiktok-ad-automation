"""Two actual PostgreSQL workers and Redis; only the HTTP boundary is synthetic."""

# ruff: noqa: F811 -- pytest shares committed fixtures between these two modules

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlmodel import Session

from app.integrations.tiktok.mcp_auth.refresh import process_mcp_refresh
from app.modules.accounts.connection_models import McpRefreshAttempt
from app.modules.accounts.models import TikTokConnection
from tests.modules.accounts.test_mcp_refresh import (  # noqa: F401
    database_engine,
    mcp_refresh_case,
    refresh_config,
    refresh_wire,
    second_refresh_actor,
)


def test_two_independent_workers_issue_exactly_one_post(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    _, connection_id, attempt_id = mcp_refresh_case
    sent, release = Event(), Event()

    def hold():
        # Independent connection proves REQUEST_ARMED committed before network entry.
        with Session(database_engine) as session:
            assert session.get(McpRefreshAttempt, attempt_id).status == "REQUEST_ARMED"
        sent.set()
        assert release.wait(10)

    refresh_wire.before = hold

    def process():
        return process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process)
        try:
            assert sent.wait(10)
            second = pool.submit(process)
            assert second.result(timeout=10) == "REQUEST_ARMED"
        finally:
            release.set()
        assert first.result(timeout=10) == "PUBLISHED"
    assert len(refresh_wire.calls) == 1
    with Session(database_engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        assert connection.credential_revision == 2
        assert connection.authorization_revision == 1


def test_two_preflights_share_one_durable_refresh(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    from datetime import UTC, datetime, timedelta

    from sqlmodel import select

    from app.core.errors import DomainError
    from app.integrations.tiktok.mcp_auth.refresh import ensure_mcp_credentials
    from app.jobs.models import PendingDispatch

    context, connection_id, attempt_id = mcp_refresh_case

    def ensure():
        try:
            ensure_mcp_credentials(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                connection_id=connection_id,
                task_deadline=datetime.now(UTC) + timedelta(seconds=30),
            )
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: ensure(), range(2))) == [
            "mcp_refresh_pending",
            "mcp_refresh_pending",
        ]
    with Session(database_engine) as session:
        dispatch = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id
            )
        ).one()
        assert dispatch.payload == {"attempt_id": str(attempt_id)}
        assert (
            len(
                session.exec(
                    select(McpRefreshAttempt).where(
                        McpRefreshAttempt.connection_id == connection_id
                    )
                ).all()
            )
            == 1
        )
    assert refresh_wire.calls == []


def test_revoked_actor_while_waiting_for_connection_lock_never_posts(
    database_engine,
    redis_client,
    mcp_refresh_case,
    refresh_wire,
    second_refresh_actor,
    monkeypatch,
):
    import os
    import time

    from sqlalchemy import text
    from sqlmodel import select

    from app.core.config import settings
    from app.core.errors import DomainError
    from app.modules.accounts.refresh_tasks import refresh_mcp
    from app.modules.tenants.models import TenantMembership

    context, connection_id, attempt_id = mcp_refresh_case
    assert redis_client.ping()
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    with Session(database_engine) as lock_session:
        lock_session.exec(
            select(TikTokConnection)
            .where(TikTokConnection.id == connection_id)
            .with_for_update()
        ).one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(
                refresh_mcp,
                tenant_id=str(context.tenant_id),
                actor_id=str(context.actor_id),
                payload={"attempt_id": str(attempt_id)},
            )
            waiting = False
            try:
                # Observe an actual PostgreSQL lock wait, without replacing any DB method.
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    with database_engine.connect() as probe:
                        waiting = bool(
                            probe.execute(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND query LIKE '%tiktok_connection%')"
                                )
                            ).scalar()
                        )
                    if waiting:
                        break
                    time.sleep(0.01)
                assert waiting
                with Session(database_engine) as revoke:
                    member = revoke.get(
                        TenantMembership, (context.tenant_id, context.actor_id)
                    )
                    member.active = False
                    revoke.add(member)
                    revoke.commit()
            finally:
                lock_session.rollback()
            try:
                worker.result(timeout=10)
            except DomainError as error:
                assert error.code == "tenant_forbidden"
    assert refresh_wire.calls == []
    with Session(database_engine) as session:
        assert session.get(McpRefreshAttempt, attempt_id).request_armed_at is None

    assert (
        process_mcp_refresh(
            database_engine=database_engine,
            redis_client=redis_client,
            context=second_refresh_actor,
            attempt_id=attempt_id,
        )
        == "PUBLISHED"
    )
    assert len(refresh_wire.calls) == 1


def test_publisher_commit_cannot_consume_worker_recovery_dispatch(
    database_engine, redis_client, mcp_refresh_case, refresh_wire, monkeypatch
):
    import os
    import time
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text
    from sqlmodel import col, select

    from app.core.config import settings
    from app.core.errors import DomainError
    from app.integrations.tiktok.mcp_auth.refresh import ensure_mcp_credentials
    from app.jobs.models import PendingDispatch
    from app.modules.accounts.refresh_tasks import refresh_mcp

    context, connection_id, attempt_id = mcp_refresh_case
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    with pytest.raises(DomainError):
        ensure_mcp_credentials(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            connection_id=connection_id,
            task_deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
    with Session(database_engine) as publisher:
        original = publisher.exec(
            select(PendingDispatch)
            .where(PendingDispatch.tenant_id == context.tenant_id)
            .with_for_update()
        ).one()
        original_id = original.id

        def check_recovery_before_http():
            with Session(database_engine) as session:
                pending = session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == context.tenant_id,
                        col(PendingDispatch.published_at).is_(None),
                    )
                ).all()
                assert len(pending) == 1 and pending[0].id != original_id

        refresh_wire.before = check_recovery_before_http
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(
                refresh_mcp,
                tenant_id=str(context.tenant_id),
                actor_id=str(context.actor_id),
                payload={"attempt_id": str(attempt_id)},
            )
            waiting = False
            try:
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    with database_engine.connect() as probe:
                        waiting = bool(
                            probe.execute(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND query LIKE '%pending_dispatch%')"
                                )
                            ).scalar()
                        )
                    if waiting or worker.done():
                        break
                    time.sleep(0.01)
            finally:
                # Mirrors real publisher: send_task already happened while published_at was still NULL.
                original.published_at = datetime.now(UTC)
                publisher.add(original)
                publisher.commit()
            result = worker.result(timeout=10)
            assert waiting
            assert result == "PUBLISHED"
    with Session(database_engine) as session:
        pending = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id,
                col(PendingDispatch.published_at).is_(None),
            )
        ).all()
        assert len(pending) == 1 and pending[0].id != original_id
    assert (
        refresh_mcp(
            tenant_id=str(context.tenant_id),
            actor_id=str(context.actor_id),
            payload={"attempt_id": str(attempt_id)},
        )
        == "PUBLISHED"
    )
    assert len(refresh_wire.calls) == 1
