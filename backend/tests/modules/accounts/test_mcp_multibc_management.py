"""真实 PostgreSQL/Redis 与本地 MCP HTTP 替身验证共享授权生命周期。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from mcp.types import CallToolResult
from sqlmodel import Session, select

from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.mcp_auth.management import (
    connection_business_centers,
    open_management_accounts,
)
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
    McpAuthorizationAttempt,
    McpRefreshAttempt,
)
from app.modules.accounts.connections import (
    add_connection_bcs,
    bind_candidate_bcs,
    disable_connection_bc,
    sync_connection_bc,
)
from app.modules.accounts.models import DiscoveryRun, TikTokConnection
from app.modules.tenants.models import TenantMembership
from tests.modules.accounts.test_mcp_binding import (  # noqa: F401
    app_config,
    bc_page,
)
from tests.modules.accounts.test_mcp_binding import (
    candidate as _candidate,
)
from tests.modules.accounts.test_mcp_binding import (
    catalog_wire as _catalog_wire,
)
from tests.modules.accounts.test_mcp_binding import (
    committed_context as _committed_context,
)
from tests.modules.accounts.test_mcp_binding import (
    oauth_wire as _oauth_wire,
)

candidate = _candidate
catalog_wire = _catalog_wire
committed_context = _committed_context
oauth_wire = _oauth_wire


def deadline():
    return datetime.now(UTC) + timedelta(seconds=30)


def bind(context, attempt_id, redis_client, bc_ids):
    return bind_candidate_bcs(
        database_engine=engine,
        redis_client=redis_client,
        context=context,
        attempt_id=attempt_id,
        bc_ids=bc_ids,
        task_deadline=deadline(),
    )


def directory(context, connection_id, redis_client, refresh=False):
    return connection_business_centers(
        database_engine=engine,
        redis_client=redis_client,
        context=context,
        connection_id=connection_id,
        task_deadline=deadline(),
        refresh=refresh,
    )


def test_two_bcs_share_one_published_credential_and_confirmation_is_idempotent(
    committed_context, candidate, catalog_wire, redis_client, oauth_wire
):
    bc_page(catalog_wire)
    connection_id, runs = bind(
        committed_context, candidate, redis_client, ["bc-1", "bc-2"]
    )
    assert {row["bc_id"] for row in runs} == {"bc-1", "bc-2"}
    assert all(row["status"] == "RUNNING" for row in runs)
    before = len(catalog_wire.calls)
    assert bind(committed_context, candidate, redis_client, ["bc-2", "bc-1"]) == (
        connection_id,
        runs,
    )
    assert len(catalog_wire.calls) == before
    assert len(oauth_wire.calls) == 1
    with Session(engine) as own:
        connection = own.get(TikTokConnection, connection_id)
        assert (connection.credential_revision, connection.authorization_revision) == (
            1,
            1,
        )
        attempt = own.get(McpAuthorizationAttempt, candidate)
        assert attempt.status == "ACCEPTED" and attempt.candidate_ciphertext is None
        assert (
            len(
                own.exec(
                    select(ConnectionAuthorization).where(
                        ConnectionAuthorization.connection_id == connection_id
                    )
                ).all()
            )
            == 1
        )
        for row in runs:
            run = own.get(DiscoveryRun, UUID(row["discovery_run_id"]))
            assert (
                run.mcp_candidate_attempt_id is None and run.authorization_revision == 1
            )
            assert run.bc_id == row["bc_id"]
    assert all(
        row["connected"]
        for row in directory(committed_context, connection_id, redis_client)
    )


def test_append_uses_current_authorization_without_oauth_or_touching_first_bc(
    committed_context, candidate, catalog_wire, redis_client, oauth_wire
):
    bc_page(catalog_wire)
    connection_id, first = bind(committed_context, candidate, redis_client, ["bc-1"])
    result = add_connection_bcs(
        database_engine=engine,
        redis_client=redis_client,
        context=committed_context,
        connection_id=connection_id,
        bc_ids=["bc-2"],
        task_deadline=deadline(),
    )
    assert result[1][0]["bc_id"] == "bc-2" and len(oauth_wire.calls) == 1
    with Session(engine) as own:
        old_run = own.get(DiscoveryRun, UUID(first[0]["discovery_run_id"]))
        assert old_run.status == "RUNNING" and old_run.authorization_revision == 1
        assert own.get(TikTokConnection, connection_id).authorization_revision == 1
    with pytest.raises(DomainError) as error:
        bind(committed_context, candidate, redis_client, ["bc-2"])
    assert error.value.code == "mcp_candidate_superseded"


def test_refresh_visible_bcs_adds_new_bc_without_reauthorizing(
    committed_context, candidate, catalog_wire, redis_client, oauth_wire
):
    bc_page(catalog_wire)
    connection_id, _ = bind(committed_context, candidate, redis_client, ["bc-1"])
    bc_page(catalog_wire, bcs=("bc-1", "bc-2", "bc-3"), total_number=3)
    rows = directory(committed_context, connection_id, redis_client, refresh=True)
    assert [row["bc_id"] for row in rows] == ["bc-1", "bc-2", "bc-3"]
    assert [row["connected"] for row in rows] == [True, False, False]
    _, runs = add_connection_bcs(
        database_engine=engine,
        redis_client=redis_client,
        context=committed_context,
        connection_id=connection_id,
        bc_ids=["bc-3"],
        task_deadline=deadline(),
    )
    assert runs[0]["bc_id"] == "bc-3" and len(oauth_wire.calls) == 1


def test_unbind_only_cancels_selected_bc_and_does_not_rotate_shared_authorization(
    committed_context, candidate, catalog_wire, redis_client
):
    bc_page(catalog_wire)
    connection_id, runs = bind(
        committed_context, candidate, redis_client, ["bc-1", "bc-2"]
    )
    with Session(engine) as own:
        for bc_id in ["bc-1", "bc-2"]:
            own.add(
                BCDefaultRoute(
                    tenant_id=committed_context.tenant_id,
                    bc_id=bc_id,
                    connection_id=connection_id,
                )
            )
        own.commit()
        disable_connection_bc(
            own, context=committed_context, connection_id=connection_id, bc_id="bc-1"
        )
        own.commit()
        first = own.get(
            BCConnectionBinding, (committed_context.tenant_id, "bc-1", connection_id)
        )
        sibling = own.get(
            BCConnectionBinding, (committed_context.tenant_id, "bc-2", connection_id)
        )
        assert first.status == "DISABLED" and first.revision == 1
        assert sibling.status == "SYNCING" and sibling.revision == 0
        assert own.get(BCDefaultRoute, (committed_context.tenant_id, "bc-1")) is None
        assert (
            own.get(BCDefaultRoute, (committed_context.tenant_id, "bc-2")) is not None
        )
        connection = own.get(TikTokConnection, connection_id)
        assert connection.status == "ACTIVE" and connection.authorization_revision == 1
        status = {
            row["bc_id"]: own.get(DiscoveryRun, UUID(row["discovery_run_id"])).status
            for row in runs
        }
        assert status == {"bc-1": "CANCELLED", "bc-2": "RUNNING"}


def test_sync_retains_binding_generation_and_default_route(
    committed_context, candidate, catalog_wire, redis_client
):
    bc_page(catalog_wire)
    connection_id, runs = bind(committed_context, candidate, redis_client, ["bc-1"])
    with Session(engine) as own:
        run = own.get(DiscoveryRun, UUID(runs[0]["discovery_run_id"]))
        binding = own.get(
            BCConnectionBinding, (committed_context.tenant_id, "bc-1", connection_id)
        )
        run.status = "COMPLETE"
        binding.status = "ACTIVE"
        own.add_all(
            [
                run,
                binding,
                BCDefaultRoute(
                    tenant_id=committed_context.tenant_id,
                    bc_id="bc-1",
                    connection_id=connection_id,
                ),
            ]
        )
        own.commit()
        new_id = sync_connection_bc(
            own, context=committed_context, connection_id=connection_id, bc_id="bc-1"
        )
        own.commit()
        assert new_id != run.id
        own.refresh(binding)
        assert binding.revision == 0 and binding.status == "SYNCING"
        assert (
            own.get(BCDefaultRoute, (committed_context.tenant_id, "bc-1")) is not None
        )
        assert (
            sync_connection_bc(
                own,
                context=committed_context,
                connection_id=connection_id,
                bc_id="bc-1",
            )
            == new_id
        )


def test_expired_shared_credentials_queue_one_refresh_for_management_reads(
    committed_context, candidate, catalog_wire, redis_client
):
    bc_page(catalog_wire)
    connection_id, _ = bind(
        committed_context, candidate, redis_client, ["bc-1", "bc-2"]
    )
    with Session(engine) as own:
        connection = own.get(TikTokConnection, connection_id)
        material = decrypt_credentials(
            tenant_id=committed_context.tenant_id,
            ciphertext=connection.credential_ciphertext,
        )
        material["expires_at"] = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=committed_context.tenant_id, value=material
        )
        own.add(connection)
        own.commit()
    before = len(catalog_wire.calls)
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            directory(committed_context, connection_id, redis_client, refresh=True)
        assert error.value.code == "mcp_refresh_pending"
    assert len(catalog_wire.calls) == before
    with Session(engine) as own:
        assert (
            len(
                own.exec(
                    select(McpRefreshAttempt).where(
                        McpRefreshAttempt.connection_id == connection_id
                    )
                ).all()
            )
            == 1
        )
        assert own.get(TikTokConnection, connection_id).authorization_revision == 1


def test_active_management_rechecks_membership_before_each_http(
    committed_context, candidate, catalog_wire, redis_client
):
    bc_page(catalog_wire)
    connection_id, _ = bind(committed_context, candidate, redis_client, ["bc-1"])
    bc_page(catalog_wire)
    with open_management_accounts(
        database_engine=engine,
        redis_client=redis_client,
        context=committed_context,
        connection_id=connection_id,
        task_deadline=deadline(),
    ) as gateway:
        gateway.business_centers(page=1, page_size=50)
        with Session(engine) as own:
            member = own.get(
                TenantMembership,
                (committed_context.tenant_id, committed_context.actor_id),
            )
            member.role = "operator"
            own.add(member)
            own.commit()
        before = len(catalog_wire.calls)
        with pytest.raises(DomainError) as error:
            gateway.business_centers(page=1, page_size=50)
        assert error.value.code == "action_forbidden"
        assert len(catalog_wire.calls) == before


def test_run_management_checks_bound_bc_generation_before_http(
    committed_context, candidate, catalog_wire, redis_client
):
    bc_page(catalog_wire)
    connection_id, runs = bind(
        committed_context, candidate, redis_client, ["bc-1", "bc-2"]
    )
    run_id = UUID(runs[0]["discovery_run_id"])
    with open_management_accounts(
        database_engine=engine,
        redis_client=redis_client,
        context=committed_context,
        connection_id=connection_id,
        run_id=run_id,
        authorization_revision=1,
        task_deadline=deadline(),
    ) as gateway:
        with Session(engine) as own:
            disable_connection_bc(
                own,
                context=committed_context,
                connection_id=connection_id,
                bc_id="bc-1",
            )
            own.commit()
        before = len(catalog_wire.calls)
        with pytest.raises(DomainError) as error:
            gateway.discovery_business_centers(page=1, page_size=50)
        assert error.value.code == "discovery_stale"
        assert len(catalog_wire.calls) == before


def test_subject_mismatch_cannot_replace_active_directory(
    committed_context, candidate, catalog_wire, redis_client
):
    bc_page(catalog_wire)
    connection_id, _ = bind(committed_context, candidate, redis_client, ["bc-1"])
    catalog_wire.enqueue_result(
        "user_info_get",
        CallToolResult(
            content=[],
            structuredContent={"code": 0, "data": {"core_user_id": "other-subject"}},
        ),
    )
    bc_page(catalog_wire)
    with pytest.raises(DomainError) as error:
        directory(committed_context, connection_id, redis_client, refresh=True)
    assert error.value.code == "mcp_authorization_mismatch"
    with Session(engine) as own:
        auth = own.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == connection_id
            )
        ).one()
        assert auth.upstream_subject == "synthetic-subject"


def test_missing_subject_keeps_candidate_unpublished(
    committed_context, candidate, catalog_wire, redis_client
):
    catalog_wire.enqueue_result(
        "user_info_get",
        CallToolResult(content=[], structuredContent={"code": 0, "data": {}}),
    )
    bc_page(catalog_wire)
    with pytest.raises(DomainError):
        bind(committed_context, candidate, redis_client, ["bc-1"])
    with Session(engine) as own:
        attempt = own.get(McpAuthorizationAttempt, candidate)
        assert attempt.status == "CANDIDATE_READY" and attempt.candidate_ciphertext
        connection = own.get(TikTokConnection, attempt.connection_id)
        assert connection.credential_revision == connection.authorization_revision == 0


def test_concurrent_same_selection_publishes_credentials_once(
    committed_context, candidate, catalog_wire, redis_client
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from tests.modules.accounts.test_mcp_binding import read_bcs

    bc_page(catalog_wire)
    read_bcs(committed_context, candidate, redis_client)
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=10)
        return bind(committed_context, candidate, redis_client, ["bc-1", "bc-2"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        first, second = [future.result(timeout=30) for future in futures]
    assert first == second
    with Session(engine) as own:
        connection = own.get(TikTokConnection, first[0])
        assert connection.authorization_revision == connection.credential_revision == 1
        assert (
            len(
                own.exec(
                    select(DiscoveryRun).where(
                        DiscoveryRun.connection_id == connection.id
                    )
                ).all()
            )
            == 2
        )


def test_reauthorization_resyncs_visible_existing_bindings_and_disables_lost_bc(
    committed_context, candidate, catalog_wire, redis_client, oauth_wire
):
    from tests.modules.accounts.test_mcp_authorization import accept, issue

    bc_page(catalog_wire)
    connection_id, _ = bind(
        committed_context, candidate, redis_client, ["bc-1", "bc-2"]
    )
    state, newer = issue(committed_context, connection_id=connection_id)
    accept(state)
    bc_page(catalog_wire, bcs=("bc-1", "bc-3"), total_number=2)
    _, runs = bind(committed_context, newer, redis_client, ["bc-3"])
    assert {run["bc_id"] for run in runs} == {"bc-1", "bc-3"}
    assert len(oauth_wire.calls) == 2
    with Session(engine) as own:
        connection = own.get(TikTokConnection, connection_id)
        assert connection.authorization_revision == 2
        for bc_id in ["bc-1", "bc-3"]:
            binding = own.get(
                BCConnectionBinding, (committed_context.tenant_id, bc_id, connection_id)
            )
            assert binding.status == "SYNCING" and binding.authorization_revision == 2
        lost = own.get(
            BCConnectionBinding, (committed_context.tenant_id, "bc-2", connection_id)
        )
        assert lost.status == "DISABLED"


def test_unbind_clears_only_the_target_bc_grants(
    committed_context, candidate, catalog_wire, redis_client
):
    from sqlmodel import delete

    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess

    bc_page(catalog_wire)
    connection_id, _ = bind(
        committed_context, candidate, redis_client, ["bc-1", "bc-2"]
    )
    try:
        with Session(engine) as own:
            own.add(
                AdvertiserAccount(
                    tenant_id=committed_context.tenant_id,
                    advertiser_id="shared-advertiser",
                )
            )
            own.flush()
            for bc_id in ["bc-1", "bc-2"]:
                own.add(
                    BCAccountAccess(
                        tenant_id=committed_context.tenant_id,
                        bc_id=bc_id,
                        connection_id=connection_id,
                        advertiser_id="shared-advertiser",
                        in_bc=True,
                        authorized=True,
                        active=True,
                        can_upload=True,
                        can_build=True,
                    )
                )
            own.commit()
            disable_connection_bc(
                own,
                context=committed_context,
                connection_id=connection_id,
                bc_id="bc-1",
            )
            own.commit()
            first = own.get(
                BCAccountAccess,
                (
                    committed_context.tenant_id,
                    "bc-1",
                    "shared-advertiser",
                    connection_id,
                ),
            )
            sibling = own.get(
                BCAccountAccess,
                (
                    committed_context.tenant_id,
                    "bc-2",
                    "shared-advertiser",
                    connection_id,
                ),
            )
            assert not any(
                [
                    first.in_bc,
                    first.authorized,
                    first.active,
                    first.can_upload,
                    first.can_build,
                ]
            )
            assert all(
                [
                    sibling.in_bc,
                    sibling.authorized,
                    sibling.active,
                    sibling.can_upload,
                    sibling.can_build,
                ]
            )
    finally:
        with Session(engine) as own:
            own.exec(
                delete(BCAccountAccess).where(
                    BCAccountAccess.tenant_id == committed_context.tenant_id
                )
            )
            own.exec(
                delete(AdvertiserAccount).where(
                    AdvertiserAccount.tenant_id == committed_context.tenant_id
                )
            )
            own.commit()


def test_adding_already_active_bc_does_not_resync_it(
    committed_context, candidate, catalog_wire, redis_client
):
    bc_page(catalog_wire)
    connection_id, original = bind(committed_context, candidate, redis_client, ["bc-1"])
    with Session(engine) as own:
        binding = own.get(
            BCConnectionBinding, (committed_context.tenant_id, "bc-1", connection_id)
        )
        run = own.get(DiscoveryRun, UUID(original[0]["discovery_run_id"]))
        binding.status = "ACTIVE"
        run.status = "COMPLETE"
        own.add_all([binding, run])
        own.commit()
    _, repeated = add_connection_bcs(
        database_engine=engine,
        redis_client=redis_client,
        context=committed_context,
        connection_id=connection_id,
        bc_ids=["bc-1"],
        task_deadline=deadline(),
    )
    assert repeated[0]["discovery_run_id"] == original[0]["discovery_run_id"]
    assert repeated[0]["status"] == "COMPLETE"
    with Session(engine) as own:
        assert (
            own.get(
                BCConnectionBinding,
                (committed_context.tenant_id, "bc-1", connection_id),
            ).status
            == "ACTIVE"
        )
