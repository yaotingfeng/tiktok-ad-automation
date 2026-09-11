from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.config import settings
from app.core.errors import DomainError
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.routing import (
    freeze_route,
    set_default_route,
    verify_route,
)
from app.modules.tenants.models import AuditEvent, TenantMembership


@pytest.fixture
def route_case(session, context):
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    bc = TenantBC(tenant_id=context.tenant_id, bc_id="synthetic-bc")
    account = AdvertiserAccount(
        tenant_id=context.tenant_id,
        advertiser_id="90071992547409931",
        currency="USD",
        timezone="UTC",
        remote_status="STATUS_ENABLE",
    )
    session.add_all([connection, bc, account])
    session.flush()
    authorization = ConnectionAuthorization(
        tenant_id=context.tenant_id,
        connection_id=connection.id,
        authorization_revision=0,
        scopes=["synthetic-account-read", "synthetic-build", "synthetic-upload"],
        permission_summary={
            "read_authorized": True,
            "build_authorized": True,
            "upload_authorized": True,
        },
        source="SYNTHETIC_COMPLETE_EVIDENCE",
        verified_at=datetime.now(UTC),
    )
    grant = BCAccountAccess(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        advertiser_id=account.advertiser_id,
        connection_id=connection.id,
        in_bc=True,
        authorized=True,
        active=True,
        can_upload=True,
        can_build=True,
        permission_state="VERIFIED",
        checked_at=datetime.now(UTC),
    )
    session.add_all(
        [
            authorization,
            grant,
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                connection_id=connection.id,
                kind=connection.kind,
            ),
        ]
    )
    session.flush()
    session.add(
        BCDefaultRoute(
            tenant_id=context.tenant_id, bc_id=bc.bc_id, connection_id=connection.id
        )
    )
    session.flush()
    return context, connection, grant, authorization


def frozen(session, case):
    context, _, grant, _ = case
    return freeze_route(session, context=context, bc_id=grant.bc_id)


def test_default_change_does_not_change_frozen_route(session, route_case):
    context, connection, grant, _ = route_case
    route = frozen(session, route_case)
    session.delete(session.get(BCDefaultRoute, (context.tenant_id, grant.bc_id)))
    session.flush()
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=grant.advertiser_id,
        capability="build",
    )
    assert route.connection_id == connection.id
    with pytest.raises(DomainError, match="默认"):
        frozen(session, route_case)


def test_missing_default_never_selects_another_connection(session, route_case):
    context, connection, grant, _ = route_case
    session.delete(session.get(BCDefaultRoute, (context.tenant_id, grant.bc_id)))
    session.flush()
    with pytest.raises(DomainError) as error:
        frozen(session, route_case)
    assert error.value.code == "bc_default_connection_required"
    route = freeze_route(
        session,
        context=context,
        bc_id=grant.bc_id,
        connection_id=connection.id,
    )
    assert route.connection_id == connection.id


@pytest.mark.parametrize(
    "changed", ["authorization", "contract", "disabled", "binding"]
)
def test_frozen_route_rechecks_current_connection(session, route_case, changed):
    context, connection, grant, _ = route_case
    route = frozen(session, route_case)
    if changed == "authorization":
        connection.authorization_revision += 1
    elif changed == "contract":
        connection.adapter_contract_revision = "changed"
    elif changed == "disabled":
        connection.status = "DISABLED"
    else:
        session.delete(session.get(BCDefaultRoute, (context.tenant_id, grant.bc_id)))
        session.flush()
        session.delete(
            session.get(
                BCConnectionBinding,
                (context.tenant_id, grant.bc_id, connection.id),
            )
        )
    session.flush()
    with pytest.raises(DomainError):
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=grant.advertiser_id,
            capability="upload",
        )


def test_credential_rotation_and_identical_evidence_keep_route(session, route_case):
    context, connection, grant, authorization = route_case
    route = frozen(session, route_case)
    connection.credential_revision += 1
    grant.checked_at = datetime.now(UTC)
    authorization.verified_at = datetime.now(UTC)
    session.flush()
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=grant.advertiser_id,
        capability="build",
    )
    assert frozen(session, route_case) == route


@pytest.mark.parametrize("kind", ["grant", "authorization"])
def test_expired_evidence_blocks_accounts_but_not_bc_recheck(session, route_case, kind):
    context, _, grant, authorization = route_case
    route = frozen(session, route_case)
    stale = datetime.now(UTC) - timedelta(
        seconds=settings.BC_CAPABILITY_MAX_AGE_SECONDS + 1
    )
    if kind == "grant":
        grant.checked_at = stale
    else:
        authorization.verified_at = stale
    session.flush()
    with pytest.raises(DomainError) as error:
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=grant.advertiser_id,
            capability="read",
        )
    assert error.value.code == "route_evidence_stale"
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )


@pytest.mark.parametrize("kind", ["scope", "role", "membership", "account", "missing"])
def test_current_scope_role_and_actor_fence_writes(session, route_case, kind):
    context, _, grant, authorization = route_case
    route = frozen(session, route_case)
    advertiser = grant.advertiser_id
    if kind == "scope":
        authorization.permission_summary = {
            **authorization.permission_summary,
            "upload_authorized": None,
        }
    elif kind == "role":
        grant.can_upload = False
    elif kind == "membership":
        session.get(
            TenantMembership, (context.tenant_id, context.actor_id)
        ).active = False
    elif kind == "account":
        advertiser = "different-advertiser"
    else:
        advertiser = None
    session.flush()
    with pytest.raises(DomainError):
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=advertiser,
            capability="upload",
        )


def test_default_management_requires_admin_and_exact_binding(
    session, route_case, other_context
):
    context, connection, grant, _ = route_case
    with pytest.raises(DomainError) as error:
        set_default_route(
            session, context=context, bc_id=grant.bc_id, connection_id=connection.id
        )
    assert error.value.code == "action_forbidden"
    session.get(
        TenantMembership, (context.tenant_id, context.actor_id)
    ).role = "tenant_admin"
    session.flush()
    with pytest.raises(DomainError):
        set_default_route(
            session, context=context, bc_id=grant.bc_id, connection_id=uuid4()
        )
    with pytest.raises(DomainError):
        set_default_route(
            session,
            context=other_context,
            bc_id=grant.bc_id,
            connection_id=connection.id,
        )
    result = set_default_route(
        session, context=context, bc_id=grant.bc_id, connection_id=connection.id
    )
    assert result.connection_id == connection.id
    events = session.exec(
        select(AuditEvent).where(AuditEvent.tenant_id == context.tenant_id)
    ).all()
    assert any(event.action == "tiktok.bc.default_connection" for event in events)
