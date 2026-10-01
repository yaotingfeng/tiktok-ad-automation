from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.modules.accounts.management_capability_models import ManagementCapability
from app.modules.accounts.routing import verify_route
from app.modules.ad_management.models import ManagementPreview
from app.modules.ad_management.schemas import MutationSpec
from app.modules.tenants.permissions import require_tenant
from tests.modules.conftest import create_context


def test_status_modes_and_management_permission(session, management_env):
    context, bc, account, route, selection = management_env
    with pytest.raises(ValidationError):
        MutationSpec(field="status", mode="increase_percent", value="ENABLE")
    with pytest.raises(ValidationError):
        MutationSpec(field="budget", mode="set", value=0)

    viewer = create_context(session, role="viewer")
    with pytest.raises(DomainError, match="不能执行"):
        require_tenant(
            session,
            actor_id=viewer.actor_id,
            tenant_id=viewer.tenant_id,
            action="ads_manage",
        )
    assert (
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="ads_manage",
        ).role
        == "operator"
    )

    with pytest.raises(DomainError, match="management_permission_unverified"):
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=account.advertiser_id,
            capability="ads_manage",
        )

    session.add(
        ManagementCapability(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            advertiser_id=account.advertiser_id,
            connection_id=route.connection_id,
            authorization_revision=route.authorization_revision,
            binding_revision=route.binding_revision,
            adapter_contract_revision=route.adapter_contract_revision,
            operation="update_roas",
            entity_kind="adgroup",
            state="VERIFIED",
            verified_at=datetime.now(UTC),
            evidence={"role": "ADMIN", "scope": ["management"]},
        )
    )
    session.flush()
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=account.advertiser_id,
        capability="ads_manage",
    )


def test_management_preview_cross_tenant_fk_and_expiry(session, management_env):
    context, bc, account, route, selection = management_env
    other = create_context(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ManagementPreview(
                tenant_id=other.tenant_id,
                bc_id=bc.bc_id,
                actor_id=other.actor_id,
                selection_id=selection.id,
                digest="d" * 64,
                route=route.model_dump(mode="json"),
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                mutation={},
                counts={},
            )
        )
        session.flush()

    preview = ManagementPreview(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        actor_id=context.actor_id,
        selection_id=selection.id,
        digest="d" * 64,
        route=route.model_dump(mode="json"),
        mutation={},
        counts={},
    )
    session.add(preview)
    session.flush()
    assert preview.expires_at > preview.created_at


@pytest.mark.parametrize("change", ["state", "binding", "verified_at", "remote_status"])
def test_management_grants_reject_incomplete_or_stale_evidence(
    session, management_env, change
):
    from app.modules.accounts.access import usable_grants

    context, bc, account, route, _ = management_env
    cap = ManagementCapability(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        advertiser_id=account.advertiser_id,
        connection_id=route.connection_id,
        authorization_revision=route.authorization_revision,
        binding_revision=route.binding_revision,
        adapter_contract_revision=route.adapter_contract_revision,
        operation="update_roas",
        entity_kind="adgroup",
        state="VERIFIED",
        verified_at=datetime.now(UTC),
        evidence={
            "role": "ADMIN",
            "scopes": ["management"],
            "interfaces": ["adgroup/update"],
        },
    )
    if change == "state":
        cap.state = "UNKNOWN"
    elif change == "binding":
        cap.binding_revision += 1
    elif change == "verified_at":
        cap.verified_at = None
    else:
        account.remote_status = "DISABLED"
        session.add(account)
    session.add(cap)
    session.flush()
    with pytest.raises(DomainError):
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=account.advertiser_id,
            capability="ads_manage",
        )
    assert not session.exec(
        usable_grants(tenant_id=context.tenant_id, bc_id=bc.bc_id, action="ads_manage")
    ).all()
