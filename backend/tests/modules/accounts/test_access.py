import pytest
from sqlalchemy import update

from app.core.errors import DomainError
from app.models import User
from app.modules.accounts.access import assign_upload_account, resolve_account_access
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.tenants.models import TenantMembership


@pytest.fixture
def account_access_case(session, context):
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    bc = TenantBC(tenant_id=context.tenant_id, bc_id="1234567890123456789", name="BC")
    account = AdvertiserAccount(
        tenant_id=context.tenant_id,
        advertiser_id="90071992547409931",
        name="Upload",
        currency="USD",
        timezone="UTC",
        remote_status="STATUS_ENABLE",
    )
    session.add_all([connection, bc, account])
    session.flush()
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
    )
    session.add(grant)
    session.flush()
    return context, grant


@pytest.mark.parametrize("field", ["in_bc", "authorized", "active", "can_upload"])
def test_upload_requires_all_access_facts(session, account_access_case, field):
    context, grant = account_access_case
    setattr(grant, field, False)
    session.flush()
    with pytest.raises(DomainError):
        resolve_account_access(
            session,
            context=context,
            bc_id=grant.bc_id,
            advertiser_id=grant.advertiser_id,
            action="upload",
        )


def test_upload_account_is_system_assigned(session, account_access_case):
    context, grant = account_access_case
    result = assign_upload_account(session, context=context, bc_id=grant.bc_id)
    assert result.advertiser_id == grant.advertiser_id
    assert result.connection_id == grant.connection_id
    grant.can_upload = False
    session.flush()
    with pytest.raises(DomainError) as error:
        assign_upload_account(session, context=context, bc_id=grant.bc_id)
    assert error.value.code == "no_upload_account"


def test_query_reloads_grants_and_roles(session, account_access_case):
    context, grant = account_access_case
    resolve_account_access(
        session,
        context=context,
        bc_id=grant.bc_id,
        advertiser_id=grant.advertiser_id,
        action="build",
    )
    session.execute(
        update(BCAccountAccess)
        .where(BCAccountAccess.tenant_id == context.tenant_id)
        .values(can_build=False)
        .execution_options(synchronize_session=False)
    )
    assert grant.can_build is True
    with pytest.raises(DomainError):
        resolve_account_access(
            session,
            context=context,
            bc_id=grant.bc_id,
            advertiser_id=grant.advertiser_id,
            action="build",
        )
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "viewer"
    session.flush()
    with pytest.raises(DomainError) as error:
        assign_upload_account(session, context=context, bc_id=grant.bc_id)
    assert error.value.code == "action_forbidden"


@pytest.mark.parametrize("target", ["bc", "account"])
def test_platform_cannot_bypass_conflicts(session, account_access_case, target):
    context, grant = account_access_case
    actor = session.get(User, context.actor_id)
    actor.is_superuser = True
    if target == "bc":
        session.get(
            TenantBC, (context.tenant_id, grant.bc_id)
        ).ownership_conflict = True
    else:
        session.get(
            AdvertiserAccount, (context.tenant_id, grant.advertiser_id)
        ).ownership_conflict = True
    session.flush()
    with pytest.raises(DomainError) as error:
        resolve_account_access(
            session,
            context=context,
            bc_id=grant.bc_id,
            advertiser_id=grant.advertiser_id,
            action="build",
        )
    assert error.value.code == "account_ownership_conflict"
    with pytest.raises(DomainError):
        assign_upload_account(session, context=context, bc_id=grant.bc_id)


def test_only_valid_connection_selected_and_not_other_bc(session, account_access_case):
    context, grant = account_access_case
    session.get(TikTokConnection, grant.connection_id).status = "DISABLED"
    valid = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(valid)
    session.flush()
    session.add(BCAccountAccess(**{**grant.model_dump(), "connection_id": valid.id}))
    session.flush()
    result = resolve_account_access(
        session,
        context=context,
        bc_id=grant.bc_id,
        advertiser_id=grant.advertiser_id,
        action="upload",
    )
    assert result.connection_id == valid.id
    with pytest.raises(DomainError) as error:
        resolve_account_access(
            session,
            context=context,
            bc_id="different",
            advertiser_id=grant.advertiser_id,
            action="upload",
        )
    assert error.value.code == "account_not_in_bc"


@pytest.mark.parametrize(
    "field,value",
    [
        ("currency", ""),
        ("timezone", ""),
        ("remote_status", "UNKNOWN"),
        ("remote_status", "STATUS_DISABLE"),
    ],
)
def test_incomplete_or_unusable_account_never_assigned(
    session, account_access_case, field, value
):
    context, grant = account_access_case
    setattr(
        session.get(AdvertiserAccount, (context.tenant_id, grant.advertiser_id)),
        field,
        value,
    )
    session.flush()
    with pytest.raises(DomainError):
        resolve_account_access(
            session,
            context=context,
            bc_id=grant.bc_id,
            advertiser_id=grant.advertiser_id,
            action="upload",
        )
    with pytest.raises(DomainError):
        assign_upload_account(session, context=context, bc_id=grant.bc_id)


def test_cross_tenant_and_unknown_action_denied(
    session, account_access_case, other_context
):
    _, grant = account_access_case
    with pytest.raises(DomainError) as error:
        resolve_account_access(
            session,
            context=other_context,
            bc_id=grant.bc_id,
            advertiser_id=grant.advertiser_id,
            action="read",
        )
    assert error.value.code == "account_not_in_bc"
    with pytest.raises(DomainError) as error:
        resolve_account_access(
            session,
            context=other_context,
            bc_id=grant.bc_id,
            advertiser_id=grant.advertiser_id,
            action="manage",
        )
    assert error.value.code == "invalid_account_action"


@pytest.mark.parametrize("permission", ["UNKNOWN", "METADATA_INCOMPLETE"])
def test_writes_require_verified_permission(session, account_access_case, permission):
    context, grant = account_access_case
    grant.permission_state = permission
    session.flush()
    with pytest.raises(DomainError):
        resolve_account_access(
            session,
            context=context,
            bc_id=grant.bc_id,
            advertiser_id=grant.advertiser_id,
            action="build",
        )
    with pytest.raises(DomainError):
        assign_upload_account(session, context=context, bc_id=grant.bc_id)


def test_upload_selection_is_bounded_join_not_account_iteration(
    session, account_access_case
):
    from sqlalchemy import event

    context, grant = account_access_case
    statements = []
    connection = session.connection()

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", observe)
    try:
        assign_upload_account(session, context=context, bc_id=grant.bc_id)
    finally:
        event.remove(connection, "before_cursor_execute", observe)
    selected = [
        statement for statement in statements if "FROM bc_account_access" in statement
    ]
    assert len(selected) == 2
    assert all(
        "LIMIT" in statement
        and "JOIN tiktok_connection" in statement
        and "JOIN advertiser_account" in statement
        for statement in selected
    )
