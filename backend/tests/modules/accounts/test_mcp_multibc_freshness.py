"""共享主体的新鲜度不能替代某个 BC 的完整账户目录证据。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.config import settings
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from app.modules.accounts.models import DiscoveryRun, TenantBC, TikTokConnection
from app.modules.accounts.runtime_directory import needs_directory_refresh
from tests.modules.conftest import create_context


@pytest.fixture
def freshness_case(session):
    context = create_context(session, role="tenant_admin")
    connection = TikTokConnection(
        tenant_id=context.tenant_id,
        kind="OFFICIAL_MCP",
        status="ACTIVE",
        authorization_revision=7,
    )
    session.add(connection)
    session.flush()
    facts = ConnectionAuthorization(
        tenant_id=context.tenant_id,
        connection_id=connection.id,
        authorization_revision=7,
        verified_at=datetime.now(UTC),
    )
    session.add(facts)
    routes = {}
    for bc_id in ["bc-a", "bc-b"]:
        session.add(TenantBC(tenant_id=context.tenant_id, bc_id=bc_id))
        session.flush()
        session.add(
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id=bc_id,
                connection_id=connection.id,
                kind="OFFICIAL_MCP",
                authorization_revision=7,
                status="ACTIVE",
            )
        )
        routes[bc_id] = FrozenTikTokRoute(
            tenant_id=context.tenant_id,
            bc_id=bc_id,
            connection_id=connection.id,
            channel="OFFICIAL_MCP",
            authorization_revision=7,
            adapter_contract_revision=connection.adapter_contract_revision,
        )
    session.flush()
    return context, connection, facts, routes


def complete(session, context, route, *, age=0, **changes):
    values = {
        "tenant_id": context.tenant_id,
        "actor_id": context.actor_id,
        "connection_id": route.connection_id,
        "bc_id": route.bc_id,
        "authorization_revision": route.authorization_revision,
        "binding_revision": route.binding_revision,
        "status": "COMPLETE",
        "completed_at": datetime.now(UTC) - timedelta(seconds=age),
        "work": {"stage": "FINALIZE", "bc_id": route.bc_id},
    }
    values.update(changes)
    run = DiscoveryRun(**values)
    session.add(run)
    session.flush()
    return run


def test_other_bc_completion_does_not_make_stale_bc_directory_fresh(
    session, freshness_case
):
    context, _, _, routes = freshness_case
    complete(session, context, routes["bc-a"])
    complete(
        session, context, routes["bc-b"], age=settings.BC_CAPABILITY_MAX_AGE_SECONDS + 1
    )
    assert not needs_directory_refresh(session, routes["bc-a"])
    assert needs_directory_refresh(session, routes["bc-b"])


@pytest.mark.parametrize(
    "changes",
    [
        {"authorization_revision": 6},
        {"binding_revision": 1},
        {"status": "ERROR"},
        {"completed_at": None},
    ],
)
def test_unmatched_or_incomplete_target_bc_evidence_requires_full_discovery(
    session, freshness_case, changes
):
    context, _, _, routes = freshness_case
    complete(session, context, routes["bc-a"], **changes)
    assert needs_directory_refresh(session, routes["bc-a"])


def test_another_connection_cannot_supply_target_bc_freshness(session, freshness_case):
    context, _, _, routes = freshness_case
    other = TikTokConnection(
        tenant_id=context.tenant_id,
        kind="OFFICIAL_MCP",
        status="ACTIVE",
        authorization_revision=7,
    )
    session.add(other)
    session.flush()
    complete(session, context, routes["bc-a"], connection_id=other.id)
    assert needs_directory_refresh(session, routes["bc-a"])


def test_empty_complete_bc_and_normal_token_rotation_keep_directory_fresh(
    session, freshness_case
):
    context, connection, _, routes = freshness_case
    complete(session, context, routes["bc-a"])
    connection.credential_revision += 1
    session.add(connection)
    session.flush()
    assert not needs_directory_refresh(session, routes["bc-a"])


def test_expired_shared_subject_still_requires_observation(session, freshness_case):
    context, _, facts, routes = freshness_case
    complete(session, context, routes["bc-a"])
    facts.verified_at = datetime.now(UTC) - timedelta(
        seconds=settings.BC_CAPABILITY_MAX_AGE_SECONDS + 1
    )
    session.add(facts)
    session.flush()
    assert needs_directory_refresh(session, routes["bc-a"])


def test_api_freshness_keeps_existing_authorization_semantics(session):
    context = create_context(session, role="tenant_admin")
    connection = TikTokConnection(
        tenant_id=context.tenant_id, kind="OFFICIAL_API", status="ACTIVE"
    )
    session.add(connection)
    session.flush()
    facts = ConnectionAuthorization(
        tenant_id=context.tenant_id,
        connection_id=connection.id,
        authorization_revision=0,
        verified_at=datetime.now(UTC),
    )
    session.add(facts)
    session.flush()
    route = FrozenTikTokRoute(
        tenant_id=context.tenant_id,
        bc_id=str(uuid4()),
        connection_id=connection.id,
        channel="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
    )
    assert not needs_directory_refresh(session, route)
    facts.verified_at = datetime.now(UTC) - timedelta(
        seconds=settings.BC_CAPABILITY_MAX_AGE_SECONDS + 1
    )
    session.add(facts)
    session.flush()
    assert needs_directory_refresh(session, route)
