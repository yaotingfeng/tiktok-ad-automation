"""真实 PostgreSQL 验证绑定代数与历史冻结路由；远端仅使用本地 HTTP fixture。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.mcp_auth.refresh import (
    ensure_mcp_credentials,
    process_mcp_refresh,
)
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    McpRefreshAttempt,
)
from app.modules.accounts.connections import (
    add_connection_bcs,
    disable_connection_bc,
    sync_connection_bc,
)
from app.modules.accounts.models import DiscoveryRun, TenantBC, TikTokConnection
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.materials.models import UploadBatch
from tests.migration_database import historical_database
from tests.modules.accounts import test_mcp_multibc_discovery as directory_fixtures
from tests.modules.conftest import create_context

app_config = directory_fixtures.app_config
catalog_wire = directory_fixtures.catalog_wire
committed_context = directory_fixtures.committed_context
directory_context = directory_fixtures.directory_context
multi_bc = directory_fixtures.multi_bc
oauth_wire = directory_fixtures.oauth_wire
enqueue_directory = directory_fixtures.enqueue_directory
finish = directory_fixtures.finish
remote_page = directory_fixtures.remote_page
response = directory_fixtures.response


@pytest.fixture
def active_bcs(multi_bc, catalog_wire, redis_client):
    context, connection_id, bcs, run_ids = multi_bc
    for bc_id, run_id in zip(bcs, run_ids, strict=True):
        enqueue_directory(catalog_wire, bc_id, (f"current-{bc_id}",))
        assert finish(context, run_id, redis_client)[0] == "COMPLETE"
    with Session(engine) as session:
        routes = tuple(
            freeze_route(session, context=context, bc_id=bc_id) for bc_id in bcs
        )
    return context, connection_id, bcs, routes


def verify_bc(session, context, route):
    # 这里只验证冻结归属；广告账户权限的独立证据由原有路由测试覆盖。
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )


def test_unbind_rebind_never_resurrects_old_route_or_changes_sibling(
    active_bcs, catalog_wire, redis_client
):
    context, connection_id, bcs, routes = active_bcs
    old, sibling = routes
    with Session(engine) as session:
        disable_connection_bc(
            session, context=context, connection_id=connection_id, bc_id=bcs[0]
        )
        session.commit()
        binding = session.get(
            BCConnectionBinding, (context.tenant_id, bcs[0], connection_id)
        )
        assert binding.status == "DISABLED"
        assert binding.revision == old.binding_revision + 1
        disabled_revision = binding.revision
        assert session.get(BCDefaultRoute, (context.tenant_id, bcs[0])) is None
        verify_bc(session, context, sibling)
        with pytest.raises(DomainError):
            verify_bc(session, context, old)
        # 重复解绑是幂等请求，不再递增绑定代数。
        disable_connection_bc(
            session, context=context, connection_id=connection_id, bc_id=bcs[0]
        )
        session.commit()
        assert (
            session.get(
                BCConnectionBinding, (context.tenant_id, bcs[0], connection_id)
            ).revision
            == disabled_revision
        )
    response(catalog_wire, "user_info_get", {"core_user_id": "synthetic-subject"})
    response(
        catalog_wire,
        "bc_get",
        remote_page([{"bc_info": {"bc_id": bc}} for bc in bcs]),
    )
    identity, runs = add_connection_bcs(
        database_engine=engine,
        redis_client=redis_client,
        context=context,
        connection_id=connection_id,
        bc_ids=[bcs[0]],
        task_deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    assert identity == connection_id
    run_id = UUID(runs[0]["discovery_run_id"])
    enqueue_directory(catalog_wire, bcs[0], (f"rebound-{bcs[0]}",))
    assert finish(context, run_id, redis_client)[0] == "COMPLETE"
    with Session(engine) as session:
        current = freeze_route(session, context=context, bc_id=bcs[0])
        assert current.binding_revision > disabled_revision
        assert current.connection_id == old.connection_id
        assert current.authorization_revision == old.authorization_revision
        assert current.adapter_contract_revision == old.adapter_contract_revision
        assert (
            session.get(DiscoveryRun, run_id).binding_revision
            == current.binding_revision
        )
        verify_bc(session, context, current)
        verify_bc(session, context, sibling)
        with pytest.raises(DomainError) as error:
            verify_bc(session, context, old)
        assert error.value.code == "route_binding_changed"
        assert freeze_route(session, context=context, bc_id=bcs[1]) == sibling


def test_normal_sync_and_actual_token_refresh_preserve_binding_generations(
    active_bcs, catalog_wire, redis_client, oauth_wire
):
    context, connection_id, bcs, routes = active_bcs
    with Session(engine) as session:
        run_id = sync_connection_bc(
            session, context=context, connection_id=connection_id, bc_id=bcs[0]
        )
        session.commit()
        run = session.get(DiscoveryRun, run_id)
        assert run.binding_revision == routes[0].binding_revision
        assert (
            session.get(
                BCConnectionBinding, (context.tenant_id, bcs[0], connection_id)
            ).revision
            == routes[0].binding_revision
        )
    enqueue_directory(catalog_wire, bcs[0], (f"synced-{bcs[0]}",))
    assert finish(context, run_id, redis_client)[0] == "COMPLETE"
    with Session(engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        credential_revision = connection.credential_revision
        material = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
        )
        material["expires_at"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id, value=material
        )
        session.add(connection)
        session.commit()
    with pytest.raises(DomainError) as error:
        ensure_mcp_credentials(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            connection_id=connection_id,
            task_deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
    assert error.value.code == "mcp_refresh_pending"
    with Session(engine) as session:
        attempt_id = session.exec(
            select(McpRefreshAttempt.id).where(
                McpRefreshAttempt.connection_id == connection_id
            )
        ).one()
    assert (
        process_mcp_refresh(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            attempt_id=attempt_id,
        )
        == "PUBLISHED"
    )
    assert len(oauth_wire.calls) == 1
    with Session(engine) as session:
        assert (
            session.get(TikTokConnection, connection_id).credential_revision
            == credential_revision + 1
        )
        for route in routes:
            assert freeze_route(session, context=context, bc_id=route.bc_id) == route
            verify_bc(session, context, route)
            assert (
                session.get(
                    BCConnectionBinding,
                    (context.tenant_id, route.bc_id, connection_id),
                ).revision
                == route.binding_revision
            )


def legacy_route(context, connection_id, bc_id="revision-bc"):
    return {
        "tenant_id": str(context.tenant_id),
        "bc_id": bc_id,
        "connection_id": str(connection_id),
        "channel": "OFFICIAL_MCP",
        "authorization_revision": 7,
        "adapter_contract_revision": "synthetic-contract",
    }


def test_legacy_six_fields_mean_generation_zero_and_extra_fields_fail(context):
    value = legacy_route(context, uuid4())
    frozen = FrozenTikTokRoute.model_validate(value)
    assert frozen.binding_revision == 0
    assert "binding_revision" not in value
    assert (
        FrozenTikTokRoute.model_validate(
            {**value, "binding_revision": 3}
        ).binding_revision
        == 3
    )
    for invalid in (-1, 1.5, "3", True, None):
        with pytest.raises(ValidationError):
            FrozenTikTokRoute.model_validate({**value, "binding_revision": invalid})
    with pytest.raises(ValidationError):
        FrozenTikTokRoute.model_validate({**value, "unexpected": 1})


ROUTE_CHECKS = (
    ("upload_batch", "ck_upload_batch_frozen_route"),
    ("material_asset_operation", "ck_material_asset_operation_frozen_route"),
    ("material_distribution", "ck_material_distribution_target_route"),
    ("material_distribution", "ck_material_distribution_source_route"),
    ("material_cover_job", "ck_material_cover_job_frozen_route"),
    ("ingest_session", "ck_ingest_session_frozen_route"),
    ("build_scene_job", "ck_build_scene_job_frozen_route"),
    ("draft_scene_preparation", "ck_draft_scene_preparation_frozen_route"),
    ("build_historical_read", "ck_historical_read_route_shape"),
)


@pytest.mark.parametrize("table, constraint", ROUTE_CHECKS)
def test_migrated_json_checks_accept_legacy_and_validate_new_generation(
    session, context, table, constraint
):
    # 取数据库已经迁移的 CHECK，而非复制实现中的表达式；隔离外键后逐个验证真实 PostgreSQL 语义。
    definition = session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=CAST(:table AS regclass) AND conname=:name"
        ),
        {"table": table, "name": constraint},
    ).scalar_one()
    session.execute(
        text(f"""CREATE TEMP TABLE route_revision_probe (
        tenant_id uuid, bc_id text, connection_id uuid,
        frozen_route jsonb, source_route jsonb, target_route jsonb,
        old_route jsonb, new_route jsonb, {definition}
    ) ON COMMIT DROP""")
    )
    connection_id = uuid4()
    route = legacy_route(context, connection_id)
    statement = text("""INSERT INTO route_revision_probe
        (tenant_id,bc_id,connection_id,frozen_route,source_route,target_route,old_route,new_route)
        VALUES (:tenant,:bc,:connection,CAST(:route AS jsonb),CAST(:route AS jsonb),
            CAST(:route AS jsonb),CAST(:route AS jsonb),CAST(:route AS jsonb))""")

    def insert(value):
        session.execute(
            statement,
            {
                "tenant": context.tenant_id,
                "bc": route["bc_id"],
                "connection": connection_id,
                "route": json.dumps(value),
            },
        )

    for value in (
        route,
        {**route, "binding_revision": 0},
        {**route, "binding_revision": 3},
    ):
        insert(value)
    for invalid in (-1, 1.5, "3", True, None, {}, []):
        with pytest.raises(IntegrityError), session.begin_nested():
            insert({**route, "binding_revision": invalid})
    with pytest.raises(IntegrityError), session.begin_nested():
        insert({**route, "binding_revision": 3, "unexpected": 1})


def test_upgrade_preserves_old_routes_and_replaces_single_bc_uniqueness(monkeypatch):
    with historical_database(monkeypatch, "mcp_cover_evidence") as (database, config):
        with Session(database) as session:
            context = create_context(session, role="tenant_admin")
            connection = TikTokConnection(
                tenant_id=context.tenant_id,
                kind="OFFICIAL_MCP",
                status="ACTIVE",
                authorization_revision=7,
                adapter_contract_revision="synthetic-contract",
            )
            session.add(connection)
            for bc_id in ("revision-bc", "other-bc"):
                session.add(TenantBC(tenant_id=context.tenant_id, bc_id=bc_id))
            session.flush()
            connection_id = connection.id
            session.execute(
                text("""INSERT INTO bc_connection_binding
                (tenant_id,bc_id,connection_id,kind) VALUES (:tenant,'revision-bc',:connection,'OFFICIAL_MCP')"""),
                {"tenant": context.tenant_id, "connection": connection_id},
            )
            route = legacy_route(context, connection_id)
            upload = UploadBatch(
                tenant_id=context.tenant_id,
                bc_id="revision-bc",
                actor_id=context.actor_id,
                request_id=uuid4(),
                request_digest="a" * 64,
                frozen_route=route,
            )
            session.add(upload)
            session.commit()
            upload_id = upload.id
            with pytest.raises(IntegrityError), session.begin_nested():
                session.execute(
                    text("""INSERT INTO bc_connection_binding
                    (tenant_id,bc_id,connection_id,kind) VALUES (:tenant,'other-bc',:connection,'OFFICIAL_MCP')"""),
                    {"tenant": context.tenant_id, "connection": connection_id},
                )
        command.upgrade(config, "mcp_multi_bc")
        with Session(database) as session:
            old = session.get(UploadBatch, upload_id).frozen_route
            assert old == route and "binding_revision" not in old
            assert FrozenTikTokRoute.model_validate(old).binding_revision == 0
            verify_bc(session, context, FrozenTikTokRoute.model_validate(old))
            binding = session.get(
                BCConnectionBinding, (context.tenant_id, "revision-bc", connection_id)
            )
            assert (
                binding.status,
                binding.authorization_revision,
                binding.revision,
            ) == ("ACTIVE", 7, 0)
            session.add(
                BCConnectionBinding(
                    tenant_id=context.tenant_id,
                    bc_id="other-bc",
                    connection_id=connection_id,
                    kind="OFFICIAL_MCP",
                    authorization_revision=7,
                )
            )
            session.flush()
            assert "uq_mcp_one_bc" not in {
                item["name"]
                for item in inspect(database).get_indexes("bc_connection_binding")
            }
            # 两个 BC 可分别持有执行中的发现任务；同 BC 重复任务仍由数据库拒绝。
            for bc_id in ("revision-bc", "other-bc"):
                session.add(
                    DiscoveryRun(
                        tenant_id=context.tenant_id,
                        actor_id=context.actor_id,
                        connection_id=connection_id,
                        bc_id=bc_id,
                        authorization_revision=7,
                        binding_revision=0,
                    )
                )
            session.flush()
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(
                    DiscoveryRun(
                        tenant_id=context.tenant_id,
                        actor_id=context.actor_id,
                        connection_id=connection_id,
                        bc_id="revision-bc",
                        authorization_revision=7,
                        binding_revision=0,
                    )
                )
                session.flush()
            current_route = {**route, "binding_revision": 3}
            session.add(
                UploadBatch(
                    tenant_id=context.tenant_id,
                    bc_id="revision-bc",
                    actor_id=context.actor_id,
                    request_id=uuid4(),
                    request_digest="b" * 64,
                    frozen_route=current_route,
                )
            )
            session.flush()
            assert session.get(UploadBatch, upload_id).frozen_route == route
            with pytest.raises(IntegrityError), session.begin_nested():
                binding.revision = -1
                session.add(binding)
                session.flush()


def test_runtime_scope_migration_uses_only_valid_original_frozen_evidence(monkeypatch):
    with historical_database(monkeypatch, "mcp_multi_bc") as (database, config):
        with Session(database) as session:
            context = create_context(session, role="tenant_admin")
            session.add(TenantBC(tenant_id=context.tenant_id, bc_id="revision-bc"))
            session.flush()
            cases = {
                "legacy": {},
                "current": {"binding_revision": 3},
                "wrong_tenant": {"tenant_id": str(uuid4())},
                "wrong_connection": {"connection_id": str(uuid4())},
                "wrong_channel": {"channel": "OFFICIAL_API"},
                "wrong_bc": {"bc_id": "different-bc"},
                "negative_generation": {"binding_revision": -1},
                "string_generation": {"binding_revision": "3"},
                "overflow_generation": {"binding_revision": 2147483648},
                "string_authorization": {"authorization_revision": "7"},
                "missing_contract": {"adapter_contract_revision": None},
                "extra_field": {"extra": True},
                "missing_route": None,
                "scalar_route": "invalid",
                "array_route": [],
            }
            runs = {}
            work_before = {}
            for name, changes in cases.items():
                connection = TikTokConnection(
                    tenant_id=context.tenant_id,
                    kind="OFFICIAL_MCP",
                    status="ACTIVE",
                    authorization_revision=12,
                    adapter_contract_revision="synthetic-contract",
                )
                session.add(connection)
                session.flush()
                session.add(
                    BCConnectionBinding(
                        tenant_id=context.tenant_id,
                        bc_id="revision-bc",
                        connection_id=connection.id,
                        kind="OFFICIAL_MCP",
                        authorization_revision=12,
                        revision=11,
                    )
                )
                work = {"mode": "RUNTIME_REFRESH", "bc_id": "revision-bc"}
                if changes is not None:
                    route = (
                        {**legacy_route(context, connection.id), **changes}
                        if isinstance(changes, dict)
                        else changes
                    )
                    if name == "missing_contract":
                        route.pop("adapter_contract_revision")
                    work["route"] = route
                run = DiscoveryRun(
                    tenant_id=context.tenant_id,
                    actor_id=context.actor_id,
                    connection_id=connection.id,
                    work=work,
                    claim_id=uuid4(),
                    claimed_until=datetime.now(UTC) + timedelta(minutes=5),
                )
                session.add(run)
                runs[name] = run.id
                work_before[name] = work
            session.commit()
        command.upgrade(config, "mcp_multibc_runtime")
        with Session(database) as session:
            for name, run_id in runs.items():
                run = session.get(DiscoveryRun, run_id)
                assert run.work == work_before[name], name
                if name in {"legacy", "current"}:
                    assert (
                        run.bc_id,
                        run.authorization_revision,
                        run.binding_revision,
                    ) == (
                        "revision-bc",
                        7,
                        0 if name == "legacy" else 3,
                    ), name
                    assert run.status == "RUNNING", name
                    assert run.claim_id is not None, name
                else:
                    assert run.status == "ERROR", name
                    assert run.error_code == "discovery_stale", name
                    assert run.bc_id is None, name
                    assert run.claim_id is None and run.claimed_until is None, name
