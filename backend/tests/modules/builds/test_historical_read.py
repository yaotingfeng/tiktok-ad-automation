"""New authorization may observe old effects, never resume old creation."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.accounts.connection_models import ConnectionAuthorization
from app.modules.accounts.models import TikTokConnection
from app.modules.accounts.routing import freeze_route
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.preview_models import BuildUnit
from tests.modules.builds.test_reconciliation import arm, wire
from tests.modules.builds.test_reconciliation import recon_env as recon_env


def renewed(env, step_id, *, subject="observed-subject"):
    from app.modules.tenants.models import TenantMembership

    with Session(env.engine) as session:
        member = session.get(
            TenantMembership, (env.context.tenant_id, env.context.actor_id)
        )
        member.role = "tenant_admin"
        session.add(member)
        step = session.get(ExecutionStep, step_id)
        unit = session.get(BuildUnit, step.unit_id)
        connection = session.get(TikTokConnection, unit.connection_id)
        old = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == connection.id,
                ConnectionAuthorization.authorization_revision
                == connection.authorization_revision,
            )
        ).one()
        old.upstream_subject = "observed-subject"
        old.issuer = old.issuer or "official-issuer"
        old.resource = old.resource or "official-resource"
        session.add(old)
        current = ConnectionAuthorization(
            tenant_id=env.context.tenant_id,
            connection_id=connection.id,
            authorization_revision=connection.authorization_revision + 1,
            upstream_subject=subject,
            issuer=old.issuer,
            resource=old.resource,
            scopes=list(old.scopes),
            permission_summary={"read_authorized": True},
            source="ACTUAL_DIRECTORY_READ",
            verified_at=datetime.now(UTC),
            previous_authorization_id=old.id,
        )
        session.add(current)
        connection.authorization_revision += 1
        session.add(connection)
        session.commit()
        return freeze_route(
            session,
            context=env.context,
            bc_id=unit.bc_id,
            connection_id=unit.connection_id,
        )


@pytest.mark.parametrize("historical_six_fields", [False, True])
def test_new_authorization_read_is_separate_and_idempotent(
    recon_env, monkeypatch, historical_six_fields
):
    from app.modules.builds.historical_read import (
        authorize_historical_read,
        process_historical_read,
    )
    from app.modules.builds.recovery_routes import (
        BuildHistoricalRead,
        BuildHistoricalReadPage,
    )

    env = recon_env
    step_id, body = arm(env)
    route = renewed(env, step_id)
    request_id = uuid4()
    # 用旧写入形状生成历史证据；读回时不能因新增默认代数0误判来源被改写。
    if historical_six_fields:
        from sqlalchemy import event

        def old_shape(_mapper, _connection, target):
            target.old_route = {
                k: v for k, v in target.old_route.items() if k != "binding_revision"
            }
            target.new_route = {
                k: v for k, v in target.new_route.items() if k != "binding_revision"
            }

        event.listen(BuildHistoricalRead, "before_insert", old_shape, once=True)
    with Session(env.engine) as session:
        read_id = authorize_historical_read(
            session,
            context=env.context,
            source_step_id=step_id,
            new_route=route,
            request_id=request_id,
        )
        session.commit()
        assert (
            authorize_historical_read(
                session,
                context=env.context,
                source_step_id=step_id,
                new_route=route,
                request_id=request_id,
            )
            == read_id
        )
        before = session.get(ExecutionStep, step_id).model_dump(mode="json")
    calls = wire(monkeypatch, [{**body, "campaign_id": "observed-remote"}])
    process_historical_read(
        database_engine=env.engine,
        redis_client=env.redis,
        context=env.context,
        payload={"read_id": str(read_id), "revision": 0},
    )
    with Session(env.engine) as session:
        audit = session.get(BuildHistoricalRead, read_id)
        assert audit.status == "CONFIRMED"
        assert set(audit.authorization_proof) >= {
            "old_verified_at",
            "new_verified_at",
            "grant_checked_at",
        }
        assert audit.remote_id == "observed-remote" and not audit.mismatch
        assert session.get(ExecutionStep, step_id).model_dump(mode="json") == before
        assert (
            len(
                session.exec(
                    select(BuildHistoricalReadPage).where(
                        BuildHistoricalReadPage.read_id == read_id
                    )
                ).all()
            )
            == 1
        )
    assert len(calls) == 1


@pytest.mark.parametrize("subject", [None, "different-subject"])
def test_unknown_or_changed_subject_cannot_authorize_historical_read(
    recon_env, subject
):
    from app.modules.builds.historical_read import authorize_historical_read

    env = recon_env
    step_id, _ = arm(env)
    route = renewed(env, step_id, subject=subject)
    with Session(env.engine) as session, pytest.raises(DomainError) as error:
        authorize_historical_read(
            session,
            context=env.context,
            source_step_id=step_id,
            new_route=route,
            request_id=uuid4(),
        )
    assert error.value.code == "historical_read_scope_unverified"


def test_audit_scope_json_and_immutability_are_database_enforced(
    recon_env, monkeypatch
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, IntegrityError

    from app.modules.builds.historical_read import (
        authorize_historical_read,
        process_historical_read,
    )
    from app.modules.builds.recovery_routes import (
        BuildHistoricalRead,
        BuildHistoricalReadPage,
    )

    env = recon_env
    step_id, body = arm(env)
    route = renewed(env, step_id)
    with Session(env.engine) as session, session.begin():
        identity = authorize_historical_read(
            session,
            context=env.context,
            source_step_id=step_id,
            new_route=route,
            request_id=uuid4(),
        )
    wire(monkeypatch, [{**body, "campaign_id": "audit-id"}])
    process_historical_read(
        database_engine=env.engine,
        redis_client=env.redis,
        context=env.context,
        payload={"read_id": str(identity), "revision": 0},
    )
    with Session(env.engine) as session:
        row = session.get(BuildHistoricalRead, identity)
        for mutation in (
            {"new_route": {**row.new_route, "extra": True}},
            {
                "new_route": {
                    **row.new_route,
                    "authorization_revision": str(row.new_authorization_revision),
                }
            },
            {"old_route": {**row.old_route, "bc_id": "other-bc"}},
            {"source_attempt_id": uuid4()},
            {"tenant_id": uuid4()},
        ):
            value = row.model_dump()
            value.update(id=uuid4(), request_id=uuid4(), **mutation)
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(BuildHistoricalRead(**value))
                session.flush()
        page = session.exec(
            select(BuildHistoricalReadPage).where(
                BuildHistoricalReadPage.read_id == identity
            )
        ).one()
        for statement, key in (
            (
                "UPDATE build_historical_read SET authorization_proof='{}' WHERE id=:id",
                identity,
            ),
            (
                "UPDATE build_historical_read SET expires_at=expires_at+interval '1 hour' WHERE id=:id",
                identity,
            ),
            ("DELETE FROM build_historical_read WHERE id=:id", identity),
            (
                "UPDATE build_historical_read_page SET summary='{}' WHERE id=:id",
                page.id,
            ),
            ("DELETE FROM build_historical_read_page WHERE id=:id", page.id),
        ):
            with pytest.raises(DBAPIError), session.begin_nested():
                session.execute(text(statement), {"id": key})


@pytest.mark.parametrize("populated", [False, True])
def test_recovery_migration_roundtrip_preserves_real_audit(
    recon_env, monkeypatch, populated
):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    from app.alembic.versions import (
        mcp_build_recovery_audit_reauthorized_read_only_build_ as migration,
    )
    from app.core.config import settings
    from app.modules.builds.historical_read import authorize_historical_read

    env = recon_env
    if populated:
        step_id, _ = arm(env)
        route = renewed(env, step_id)
        with Session(env.engine) as session, session.begin():
            authorize_historical_read(
                session,
                context=env.context,
                source_step_id=step_id,
                new_route=route,
                request_id=uuid4(),
            )
    backend = Path(__file__).resolve().parents[3]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "app/alembic"))
    monkeypatch.setattr(
        settings, "DATABASE_URL", env.engine.url.render_as_string(hide_password=False)
    )
    with env.engine.connect() as database:
        original_constraints = inspect(database).get_check_constraints(
            "build_historical_read"
        )
        original_audits = database.execute(
            text("SELECT to_jsonb(r) FROM build_historical_read r ORDER BY id")
        ).all()
        database.rollback()
        # 只执行被测迁移，不跨越后续多 BC 的独立降级保护。DDL 全部回滚，
        # 保留当前 schema 的七字段约束，不能用旧 upgrade 覆盖今日表结构。
        transaction = database.begin()
        try:
            with Operations.context(MigrationContext.configure(database)):
                if populated:
                    assert original_audits
                    with pytest.raises(
                        RuntimeError,
                        match="historical build read evidence cannot be downgraded",
                    ):
                        migration.downgrade()
                    assert (
                        database.execute(
                            text(
                                "SELECT to_jsonb(r) FROM build_historical_read r ORDER BY id"
                            )
                        ).all()
                        == original_audits
                    )
                else:
                    assert not original_audits
                    migration.downgrade()
                    assert (
                        "build_historical_read"
                        not in inspect(database).get_table_names()
                    )
                    migration.upgrade()
                    assert (
                        "build_historical_read" in inspect(database).get_table_names()
                    )
        finally:
            transaction.rollback()
        assert (
            inspect(database).get_check_constraints("build_historical_read")
            == original_constraints
        )
        assert (
            database.execute(
                text("SELECT to_jsonb(r) FROM build_historical_read r ORDER BY id")
            ).all()
            == original_audits
        )
    command.check(cfg)


def test_main_http_historical_request_is_tenant_scoped_and_replay_never_refreezes(
    recon_env,
):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from app.api.deps import get_current_user, get_db
    from app.jobs.models import PendingDispatch
    from app.main import app
    from app.modules.builds.recovery_routes import BuildHistoricalRead
    from app.modules.tenants.models import TenantMembership
    from tests.modules.conftest import create_context

    env = recon_env
    step_id, _ = arm(env)
    route = renewed(env, step_id)
    with Session(env.engine) as session, session.begin():
        other = create_context(session)
        session.add(
            TenantMembership(
                tenant_id=other.tenant_id,
                user_id=env.context.actor_id,
                role="tenant_admin",
            )
        )

    def database():
        with Session(env.engine) as session:
            yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=env.context.actor_id
    )
    base = f"/api/tenants/{env.context.tenant_id}"
    request_id = str(uuid4())
    try:
        with TestClient(app) as client:
            assert (
                client.get(
                    f"{base}/historical-build-read-requests/{request_id}"
                ).status_code
                == 404
            )
            first = client.post(
                f"{base}/execution-steps/{step_id}/historical-read",
                json={"request_id": request_id},
            )
            assert first.status_code == 202
            value = first.json()
            assert set(value) == {
                "read_id",
                "request_id",
                "source_step_id",
                "state",
                "remote_id",
                "mismatch",
                "reason_code",
                "requires_new_preparation",
            }
            with Session(env.engine) as session, session.begin():
                connection = session.get(TikTokConnection, route.connection_id)
                connection.authorization_revision += 1
                session.add(connection)
            assert (
                client.post(
                    f"{base}/execution-steps/{step_id}/historical-read",
                    json={"request_id": request_id},
                ).json()
                == value
            )
            assert (
                client.get(f"{base}/historical-build-read-requests/{request_id}").json()
                == value
            )
            assert (
                client.get(
                    f"/api/tenants/{other.tenant_id}/historical-build-read-requests/{request_id}"
                ).status_code
                == 404
            )
            assert (
                client.get(
                    f"/api/tenants/{other.tenant_id}/historical-build-reads/{value['read_id']}"
                ).status_code
                == 404
            )
            assert (
                client.post(
                    f"{base}/execution-steps/{step_id}/historical-read",
                    json={"request_id": str(uuid4()), "connection_id": str(uuid4())},
                ).status_code
                == 422
            )
            with Session(env.engine) as session, session.begin():
                member = session.get(
                    TenantMembership, (env.context.tenant_id, env.context.actor_id)
                )
                member.role = "viewer"
                session.add(member)
            assert (
                client.post(
                    f"{base}/execution-steps/{step_id}/historical-read",
                    json={"request_id": request_id},
                ).status_code
                == 403
            )
            assert (
                client.get(f"{base}/historical-build-read-requests/{request_id}").json()
                == value
            )
        with Session(env.engine) as session:
            assert (
                len(
                    session.exec(
                        select(BuildHistoricalRead).where(
                            BuildHistoricalRead.tenant_id == env.context.tenant_id
                        )
                    ).all()
                )
                == 1
            )
            assert (
                len(
                    session.exec(
                        select(PendingDispatch).where(
                            PendingDispatch.tenant_id == env.context.tenant_id,
                            PendingDispatch.task_name == "builds.historical_read_step",
                        )
                    ).all()
                )
                == 1
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("failure", ["duplicate_id", "later_page_error"])
def test_partial_historical_scan_never_publishes_candidate(
    recon_env, monkeypatch, failure
):
    from app.modules.builds.historical_read import (
        authorize_historical_read,
        process_historical_read,
    )
    from app.modules.builds.recovery_routes import (
        BuildHistoricalRead,
        BuildHistoricalReadPage,
    )

    env = recon_env
    step_id, body = arm(env)
    route = renewed(env, step_id)
    with Session(env.engine) as session, session.begin():
        identity = authorize_historical_read(
            session,
            context=env.context,
            source_step_id=step_id,
            new_route=route,
            request_id=uuid4(),
        )
    rows = [
        {**body, "campaign_name": f"other-{i}", "campaign_id": f"id-{i}"}
        for i in range(100)
    ] + [{**body, "campaign_id": "id-0"}]
    wire(monkeypatch, rows)
    process_historical_read(
        database_engine=env.engine,
        redis_client=env.redis,
        context=env.context,
        payload={"read_id": str(identity), "revision": 0},
    )
    if failure == "later_page_error":
        wire(monkeypatch, rows, fail=True)
    process_historical_read(
        database_engine=env.engine,
        redis_client=env.redis,
        context=env.context,
        payload={"read_id": str(identity), "revision": 1},
    )
    with Session(env.engine) as session:
        audit = session.get(BuildHistoricalRead, identity)
        assert audit.status == "UNKNOWN" and audit.remote_id is None
        assert (
            len(
                session.exec(
                    select(BuildHistoricalReadPage).where(
                        BuildHistoricalReadPage.read_id == identity
                    )
                ).all()
            )
            == 2
        )
        assert session.get(ExecutionStep, step_id).status == "UNKNOWN"


def test_historical_repair_republishes_only_exact_current_generation(
    recon_env, monkeypatch
):
    from datetime import timedelta

    from app.jobs.models import PendingDispatch
    from app.modules.builds.historical_read import (
        authorize_historical_read,
        process_historical_read,
        repair_historical_reads,
    )
    from app.modules.builds.recovery_routes import BuildHistoricalRead

    env = recon_env
    step_id, body = arm(env)
    route = renewed(env, step_id)
    with Session(env.engine) as session, session.begin():
        identity = authorize_historical_read(
            session,
            context=env.context,
            source_step_id=step_id,
            new_route=route,
            request_id=uuid4(),
        )
        row = session.get(BuildHistoricalRead, identity)
        dispatch_id = row.dispatch_id
        row.status, row.claim_token = "RUNNING", uuid4()
        row.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        row.repair_after = row.claimed_until
        dispatch = session.get(PendingDispatch, dispatch_id)
        dispatch.published_at = row.claimed_until
        session.add_all([row, dispatch])
    assert repair_historical_reads(database_engine=env.engine) == 1
    with Session(env.engine) as session:
        row = session.get(BuildHistoricalRead, identity)
        assert row.dispatch_id == dispatch_id and row.dispatch_revision == 0
        assert session.get(PendingDispatch, dispatch_id).published_at is None
    calls = wire(monkeypatch, [{**body, "campaign_id": "recovered-audit"}])
    for _ in range(2):
        process_historical_read(
            database_engine=env.engine,
            redis_client=env.redis,
            context=env.context,
            payload={"read_id": str(identity), "revision": 0},
        )
    assert len(calls) == 1
    with Session(env.engine) as session:
        assert session.get(BuildHistoricalRead, identity).status == "CONFIRMED"
        assert session.get(ExecutionStep, step_id).status == "UNKNOWN"


def test_historical_task_loader_registers_bounded_read_and_repair():
    from app.jobs.celery_app import celery_app
    from app.jobs.tasks import dispatch_queue

    celery_app.loader.import_default_modules()
    assert dispatch_queue("builds.historical_read_step") == "resources"
    task = celery_app.tasks["builds.historical_read_step"]
    assert (task.time_limit, task.soft_time_limit) == (45, 40)
    assert "builds.repair_historical_reads" in celery_app.tasks


@pytest.mark.parametrize("change", ["actor", "read_scope"])
def test_permission_lost_during_http_preserves_evidence_without_confirming(
    recon_env, monkeypatch, change
):
    from app.modules.builds.historical_read import (
        authorize_historical_read,
        process_historical_read,
    )
    from app.modules.builds.recovery_routes import (
        BuildHistoricalRead,
        BuildHistoricalReadPage,
    )
    from app.modules.tenants.models import TenantMembership

    env = recon_env
    step_id, body = arm(env)
    route = renewed(env, step_id)
    with Session(env.engine) as session, session.begin():
        identity = authorize_historical_read(
            session,
            context=env.context,
            source_step_id=step_id,
            new_route=route,
            request_id=uuid4(),
        )

    def changed(_path, _query):
        with Session(env.engine) as session, session.begin():
            if change == "actor":
                member = session.get(
                    TenantMembership, (env.context.tenant_id, env.context.actor_id)
                )
                member.role = "viewer"
                session.add(member)
            else:
                authorization = session.exec(
                    select(ConnectionAuthorization).where(
                        ConnectionAuthorization.connection_id == route.connection_id,
                        ConnectionAuthorization.authorization_revision
                        == route.authorization_revision,
                    )
                ).one()
                authorization.permission_summary = {"read_authorized": False}
                session.add(authorization)

    calls = wire(
        monkeypatch,
        [{**body, "campaign_id": "observed-before-revocation"}],
        hook=changed,
    )
    process_historical_read(
        database_engine=env.engine,
        redis_client=env.redis,
        context=env.context,
        payload={"read_id": str(identity), "revision": 0},
    )
    assert len(calls) == 1
    with Session(env.engine) as session:
        audit = session.get(BuildHistoricalRead, identity)
        assert audit.status == "BLOCKED" and audit.remote_id is None
        page = session.exec(
            select(BuildHistoricalReadPage).where(
                BuildHistoricalReadPage.read_id == identity
            )
        ).one()
        assert (
            page.ids == ["observed-before-revocation"]
            and page.call_evidence["request_id"]
        )
        assert session.get(ExecutionStep, step_id).status == "UNKNOWN"


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize(
    "remote_id",
    ["https://signed.example.test/object?signature=private", "remote\ncontrol"],
)
def test_poisoned_readback_id_never_reaches_step_or_historical_receipt(
    recon_env, monkeypatch, historical, remote_id
):
    from app.modules.builds import reconciliation
    from app.modules.builds.historical_read import (
        authorize_historical_read,
        historical_read_receipt,
        process_historical_read,
    )
    from app.modules.builds.recovery_routes import (
        BuildHistoricalRead,
        BuildHistoricalReadPage,
    )

    env = recon_env
    step_id, body = arm(env)
    if historical:
        route = renewed(env, step_id)
        with Session(env.engine) as session, session.begin():
            identity = authorize_historical_read(
                session,
                context=env.context,
                source_step_id=step_id,
                new_route=route,
                request_id=uuid4(),
            )
    wire(monkeypatch, [{**body, "campaign_id": remote_id}])
    if historical:
        process_historical_read(
            database_engine=env.engine,
            redis_client=env.redis,
            context=env.context,
            payload={"read_id": str(identity), "revision": 0},
        )
    else:
        result = reconciliation.process_reconciliation(
            database_engine=env.engine,
            redis_client=env.redis,
            context=env.context,
            step_id=step_id,
            revision=0,
        )
        assert result.state == "UNKNOWN"
    with Session(env.engine) as session:
        source = session.get(ExecutionStep, step_id)
        assert source.remote_id is None and source.status == "UNKNOWN"
        if historical:
            assert session.get(BuildHistoricalRead, identity).remote_id is None
            assert (
                remote_id
                not in historical_read_receipt(
                    session, context=env.context, read_id=identity
                ).model_dump_json()
            )
            assert all(
                not page.ids
                for page in session.exec(
                    select(BuildHistoricalReadPage).where(
                        BuildHistoricalReadPage.read_id == identity
                    )
                ).all()
            )
