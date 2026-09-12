"""真实 PostgreSQL/Redis 和本地 MCP HTTP 验证 BC 间发布与失败隔离。"""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from mcp.types import CallToolResult
from sqlmodel import Session, select

from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.mcp_auth.refresh import process_mcp_refresh
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
    ConnectionToolObservation,
    McpRefreshAttempt,
)
from app.modules.accounts.mcp_discovery import publish_mcp_directory
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    DiscoveryRun,
    TikTokConnection,
)
from app.modules.tenants.models import AuditEvent
from tests.modules.accounts.test_mcp_directory_publish import (  # noqa: F401
    app_config,
    enqueue_directory,
    finish,
    remote_page,
    response,
    seed_connection,
    seed_run,
    step,
    to_finalize,
)
from tests.modules.accounts.test_mcp_directory_publish import (
    catalog_wire as _catalog_wire,
)
from tests.modules.accounts.test_mcp_directory_publish import (
    committed_context as _committed_context,
)
from tests.modules.accounts.test_mcp_directory_publish import (
    directory_context as _directory_context,
)
from tests.modules.accounts.test_mcp_directory_publish import (
    oauth_wire as _oauth_wire,
)

catalog_wire = _catalog_wire
committed_context = _committed_context
directory_context = _directory_context
oauth_wire = _oauth_wire


@pytest.fixture
def multi_bc(directory_context):
    bcs = tuple(f"bc-{index}-{directory_context.tenant_id}" for index in (1, 2))
    with Session(engine) as session:
        connection, observation = seed_connection(session, directory_context, bcs)
        runs = tuple(
            seed_run(session, directory_context, connection, observation, bc)
            for bc in bcs
        )
        run_ids = tuple(run.id for run in runs)
        connection_id = connection.id
        # 每个 BC 已有可用历史快照，用于检验发布只清理自己的旧授权。
        for bc in bcs:
            old_id = f"old-{bc}"
            session.add(
                AdvertiserAccount(
                    tenant_id=directory_context.tenant_id,
                    advertiser_id=old_id,
                    name="Old",
                    currency="USD",
                    timezone="UTC",
                    remote_status="ENABLE",
                )
            )
            session.flush()
            session.add(
                BCAccountAccess(
                    tenant_id=directory_context.tenant_id,
                    bc_id=bc,
                    advertiser_id=old_id,
                    connection_id=connection_id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    can_upload=True,
                    can_build=True,
                    permission_state="VERIFIED",
                )
            )
        session.commit()
    return directory_context, connection_id, bcs, run_ids


def grants(session, context, connection_id):
    return {
        (grant.bc_id, grant.advertiser_id): grant
        for grant in session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == context.tenant_id,
                BCAccountAccess.connection_id == connection_id,
            )
        ).all()
    }


def test_one_bc_failure_does_not_clear_other_bc_or_shared_authorization(
    multi_bc, catalog_wire, redis_client
):
    context, connection_id, bcs, runs = multi_bc
    response(catalog_wire, "user_info_get", {"core_user_id": "synthetic-subject"})
    response(catalog_wire, "auth_advertiser_get", {"list": []})
    response(catalog_wire, "bc_get", remote_page([{"bc_info": {"bc_id": bcs[1]}}]))
    catalog_wire.enqueue_result(
        "bc_asset_get",
        CallToolResult(
            content=[],
            structuredContent={"code": 40000, "message": "synthetic failure"},
        ),
    )
    assert finish(context, runs[1], redis_client)[0] == "ERROR"
    enqueue_directory(catalog_wire, bcs[0], (f"new-{bcs[0]}",))
    assert finish(context, runs[0], redis_client)[0] == "COMPLETE"
    with Session(engine) as session:
        assert (
            session.get(
                BCConnectionBinding, (context.tenant_id, bcs[0], connection_id)
            ).status
            == "ACTIVE"
        )
        failed = session.get(
            BCConnectionBinding, (context.tenant_id, bcs[1], connection_id)
        )
        assert failed.status == "ERROR" and failed.last_error_code
        rows = grants(session, context, connection_id)
        assert rows[bcs[1], f"old-{bcs[1]}"].active
        assert rows[bcs[1], f"old-{bcs[1]}"].can_build
        assert not rows[bcs[0], f"old-{bcs[0]}"].active
        assert rows[bcs[0], f"new-{bcs[0]}"].active
        connection = session.get(TikTokConnection, connection_id)
        assert (connection.authorization_revision, connection.credential_revision) == (
            1,
            1,
        )
        assert (
            len(
                session.exec(
                    select(ConnectionAuthorization).where(
                        ConnectionAuthorization.connection_id == connection_id
                    )
                ).all()
            )
            == 1
        )


def test_concurrent_bc_publish_keeps_both_snapshots_and_publishes_once(
    multi_bc, catalog_wire, redis_client
):
    context, connection_id, bcs, runs = multi_bc
    for bc, run_id in zip(bcs, runs, strict=True):
        enqueue_directory(catalog_wire, bc, (f"new-{bc}",))
        to_finalize(context, run_id, redis_client)
    before = len(catalog_wire.calls)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda run_id: step(context, run_id, redis_client), runs)
        )
    assert all(result[0] == "COMPLETE" for result in results)
    for run_id in runs:
        assert step(context, run_id, redis_client, initial=True)[0] == "COMPLETE"
    assert len(catalog_wire.calls) == before
    with Session(engine) as session:
        rows = grants(session, context, connection_id)
        for bc in bcs:
            assert rows[bc, f"new-{bc}"].active
            assert not rows[bc, f"old-{bc}"].active
            assert (
                session.get(BCDefaultRoute, (context.tenant_id, bc)).connection_id
                == connection_id
            )
        events = session.exec(
            select(AuditEvent).where(
                AuditEvent.tenant_id == context.tenant_id,
                AuditEvent.action == "tiktok.mcp.directory.publish",
            )
        ).all()
        assert len(events) == 2


def test_duplicate_dispatch_during_http_does_not_repeat_stage(
    multi_bc, catalog_wire, redis_client
):
    context, _, _, runs = multi_bc
    response(catalog_wire, "user_info_get", {"core_user_id": "synthetic-subject"})
    catalog_wire.delay = 0.5
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(step, context, runs[0], redis_client, initial=True)
        deadline = time.monotonic() + 5
        while not any(call["method"] == "tools/call" for call in catalog_wire.calls):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        second = executor.submit(step, context, runs[0], redis_client, initial=True)
        second.result(timeout=5)
        assert first.result(timeout=5)[1] == "AUTHORIZED"
    assert sum(call["method"] == "tools/call" for call in catalog_wire.calls) == 1
    with Session(engine) as session:
        assert session.get(DiscoveryRun, runs[0]).revision == 1


@pytest.mark.parametrize("change", ["unbind", "reauthorize", "claim", "revision"])
def test_late_http_result_cannot_publish_or_mark_changed_binding_failed(
    multi_bc, catalog_wire, redis_client, change
):
    context, connection_id, bcs, runs = multi_bc
    response(catalog_wire, "user_info_get", {"core_user_id": "synthetic-subject"})
    catalog_wire.delay = 0.5
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(step, context, runs[0], redis_client)
        deadline = time.monotonic() + 5
        while not any(call["method"] == "tools/call" for call in catalog_wire.calls):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with Session(engine) as session:
            binding = session.get(
                BCConnectionBinding, (context.tenant_id, bcs[0], connection_id)
            )
            run = session.get(DiscoveryRun, runs[0])
            if change == "unbind":
                binding.status = "DISABLED"
                binding.revision += 1
            elif change == "reauthorize":
                connection = session.get(TikTokConnection, connection_id)
                connection.authorization_revision += 1
                session.add(connection)
            elif change == "claim":
                run.claim_id = uuid4()
            else:
                run.revision += 1
            session.add_all([binding, run])
            session.commit()
        pending.result(timeout=5)
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert run.work["stage"] == "MCP_VERIFY" and run.status == "RUNNING"
        assert session.get(
            BCConnectionBinding, (context.tenant_id, bcs[0], connection_id)
        ).status == ("DISABLED" if change == "unbind" else "SYNCING")
        assert (
            session.get(
                BCConnectionBinding, (context.tenant_id, bcs[1], connection_id)
            ).status
            == "SYNCING"
        )
        assert all(
            grant.active for grant in grants(session, context, connection_id).values()
        )


def test_finalize_rejects_replaced_claim(multi_bc, catalog_wire, redis_client):
    context, _, bcs, runs = multi_bc
    enqueue_directory(catalog_wire, bcs[0], ())
    to_finalize(context, runs[0], redis_client)
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        run.claim_id = uuid4()
        run.claimed_until = datetime.now(UTC) + timedelta(seconds=60)
        session.add(run)
        session.commit()
        with pytest.raises(DomainError, match="执行租约"):
            publish_mcp_directory(
                session,
                context=context,
                run_id=run.id,
                claim_id=uuid4(),
                revision=run.revision,
            )
        assert run.status == "RUNNING"


def test_normal_refresh_does_not_stale_sibling_bc_runs(
    multi_bc, catalog_wire, redis_client, oauth_wire
):
    context, connection_id, bcs, runs = multi_bc
    enqueue_directory(catalog_wire, bcs[0], (f"new-{bcs[0]}",))
    to_finalize(context, runs[0], redis_client)
    with Session(engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        material = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
        )
        material["expires_at"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id, value=material
        )
        session.add(connection)
        session.commit()
    enqueue_directory(catalog_wire, bcs[1], (f"new-{bcs[1]}",))
    assert step(context, runs[1], redis_client) == (
        "ADMISSION_WAIT",
        "MCP_VERIFY",
        "mcp_refresh_pending",
    )
    with Session(engine) as session:
        attempt = session.exec(
            select(McpRefreshAttempt).where(
                McpRefreshAttempt.connection_id == connection_id
            )
        ).one()
        attempt_id = attempt.id
    assert (
        process_mcp_refresh(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            attempt_id=attempt_id,
        )
        == "PUBLISHED"
    )
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[1])
        run.next_attempt_at = None
        session.add(run)
        session.commit()
    state = finish(context, runs[1], redis_client)
    assert state[0] == "COMPLETE", state
    assert step(context, runs[0], redis_client)[0] == "COMPLETE"
    assert len(oauth_wire.calls) == 1 and "refresh_token" in oauth_wire.calls[0]
    with Session(engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        assert (connection.credential_revision, connection.authorization_revision) == (
            2,
            1,
        )
        assert all(
            session.get(DiscoveryRun, identity).authorization_revision == 1
            for identity in runs
        )
        rows = grants(session, context, connection_id)
        assert all(rows[bc, f"new-{bc}"].active for bc in bcs)


@pytest.mark.parametrize("damage", ["authorization_revision", "connection", "subject"])
def test_active_observation_and_subject_are_bound_to_current_authorization(
    multi_bc, catalog_wire, redis_client, damage
):
    context, connection_id, bcs, runs = multi_bc
    if damage == "subject":
        enqueue_directory(catalog_wire, bcs[0], ())
        to_finalize(context, runs[0], redis_client)
        with Session(engine) as session:
            authorization = session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == connection_id
                )
            ).one()
            authorization.upstream_subject = "different-subject"
            session.add(authorization)
            session.commit()
    else:
        with Session(engine) as session:
            run = session.get(DiscoveryRun, runs[0])
            observation = session.get(
                ConnectionToolObservation,
                UUID(run.work["observation_id"]),
            )
            if damage == "authorization_revision":
                observation.call_evidence = {
                    **observation.call_evidence,
                    "authorization_revision": 0,
                }
            else:
                # 没有活动观察时不能挪用同连接另一次旧观察；改用另一连接证据。
                other, _ = seed_connection(session, context, bcs)
                observation.connection_id = other.id
            session.add(observation)
            session.commit()
    before = len(catalog_wire.calls)
    assert step(context, runs[0], redis_client)[0] == "ERROR"
    assert len(catalog_wire.calls) == before


def test_publish_preserves_existing_default_and_write_authorization(
    multi_bc, catalog_wire, redis_client
):
    context, connection_id, bcs, runs = multi_bc
    with Session(engine) as session:
        other = TikTokConnection(
            tenant_id=context.tenant_id, kind="OFFICIAL_API", status="ACTIVE"
        )
        session.add(other)
        session.flush()
        session.add(
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id=bcs[0],
                connection_id=other.id,
                kind="OFFICIAL_API",
            )
        )
        session.flush()
        session.add(
            BCDefaultRoute(
                tenant_id=context.tenant_id, bc_id=bcs[0], connection_id=other.id
            )
        )
        authorization = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == connection_id
            )
        ).one()
        authorization.permission_summary = {
            "read_authorized": None,
            "build_authorized": True,
            "upload_authorized": False,
        }
        session.add(authorization)
        other_id = other.id
        session.commit()
    enqueue_directory(catalog_wire, bcs[0], ())
    assert finish(context, runs[0], redis_client)[0] == "COMPLETE"
    with Session(engine) as session:
        assert (
            session.get(BCDefaultRoute, (context.tenant_id, bcs[0])).connection_id
            == other_id
        )
        authorization = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == connection_id
            )
        ).one()
        assert authorization.permission_summary == {
            "read_authorized": True,
            "build_authorized": True,
            "upload_authorized": False,
        }


def test_runtime_scans_are_independent_and_preserve_other_bc_evidence(
    multi_bc, catalog_wire, redis_client, monkeypatch
):
    from app.core.config import settings
    from app.modules.accounts import capabilities
    from tests.modules.accounts.capabilities.test_service import run as run_capability
    from tests.modules.accounts.capabilities.test_service import start
    from tests.modules.accounts.test_runtime_directory import finish_runtime

    context, connection_id, bcs, runs = multi_bc
    monkeypatch.setattr(settings, "BC_CAPABILITY_MAX_AGE_SECONDS", 14400)
    monkeypatch.setattr(capabilities, "_require_bounded_worker", lambda: None)
    for bc, run_id in zip(bcs, runs, strict=True):
        enqueue_directory(catalog_wire, bc, (f"new-{bc}",))
        assert finish(context, run_id, redis_client)[0] == "COMPLETE"
    with Session(engine) as session:
        authorization = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == connection_id
            )
        ).one()
        authorization.verified_at = datetime.now(UTC) - timedelta(hours=5)
        authorization.permission_summary = {
            "read_authorized": True,
            "build_authorized": True,
            "upload_authorized": True,
        }
        session.add(authorization)
        other_grant = session.get(
            BCAccountAccess, (context.tenant_id, bcs[1], f"new-{bcs[1]}", connection_id)
        )
        other_grant.can_build = other_grant.can_upload = True
        session.add(other_grant)
        session.commit()
    environments = []
    for bc in bcs:
        env = {
            "context": context,
            "connection_id": connection_id,
            "bc_id": bc,
            "advertiser_id": f"new-{bc}",
            "request_id": uuid4(),
        }
        job_id = start(env)
        assert run_capability(env, redis_client, job_id).status == "PENDING"
        env["job_id"] = job_id
        environments.append(env)
    with Session(engine) as session:
        runtime_runs = session.exec(
            select(DiscoveryRun).where(
                DiscoveryRun.connection_id == connection_id,
                DiscoveryRun.work["mode"].as_string() == "RUNTIME_REFRESH",
            )
        ).all()
        assert len(runtime_runs) == 2
        by_bc = {run.bc_id: run for run in runtime_runs}
        assert set(by_bc) == set(bcs)
        assert all(
            run.authorization_revision == 1 and run.binding_revision == 1
            for run in runtime_runs
        )
        run_ids = [by_bc[bc].id for bc in bcs]
    # 目标 BC 的完整授权回执不含另一 BC；MCP 仍只能发布当前 BC。
    enqueue_directory(catalog_wire, bcs[0], (f"new-{bcs[0]}",))
    completed = finish_runtime(environments[0], redis_client, run_ids[0])
    assert completed.status == "COMPLETE", completed.error_code
    with Session(engine) as session:
        assert session.get(DiscoveryRun, run_ids[1]).status == "RUNNING"
        other_grant = session.get(
            BCAccountAccess, (context.tenant_id, bcs[1], f"new-{bcs[1]}", connection_id)
        )
        assert other_grant.authorized and other_grant.active
        assert other_grant.can_build and other_grant.can_upload
        authorization = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == connection_id
            )
        ).one()
        assert authorization.permission_summary == {
            "read_authorized": True,
            "build_authorized": True,
            "upload_authorized": True,
        }
    enqueue_directory(catalog_wire, bcs[1], (f"new-{bcs[1]}",))
    completed = finish_runtime(environments[1], redis_client, run_ids[1])
    assert completed.status == "COMPLETE", completed.error_code
