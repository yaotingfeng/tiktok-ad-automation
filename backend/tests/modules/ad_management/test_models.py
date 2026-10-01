from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.modules.accounts.management_capability_models import ManagementCapability
from app.modules.accounts.routing import verify_route
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementPreviewItem,
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
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
        operation="update_roas",
        entity_kind="adgroup",
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

    # The selection must belong to the same BC as the preview, even within one
    # tenant.  The tenant-only FK from the original revision was insufficient.
    from app.modules.accounts.models import TenantBC
    from app.modules.reporting.query_models import FrozenSelectionRecord

    other_bc = TenantBC(tenant_id=context.tenant_id, bc_id="management-bc-other")
    session.add(other_bc)
    session.flush()
    other_selection = FrozenSelectionRecord(
        tenant_id=context.tenant_id,
        bc_id=other_bc.bc_id,
        actor_id=context.actor_id,
        snapshot_id=uuid4(),
        advertiser_ids=[account.advertiser_id],
        filters={},
        filter_digest="g" * 64,
        publication_versions={},
        naming_versions={},
        refs=[],
        material_uses=[],
        membership_digest="n" * 64,
    )
    session.add(other_selection)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ManagementPreview(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                actor_id=context.actor_id,
                selection_id=other_selection.id,
                digest="x" * 64,
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


def test_capability_route_fk_and_request_attempt_number_are_database_fenced(
    session, management_env
):
    context, bc, account, route, selection = management_env
    capability = ManagementCapability(
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
        evidence={"role": "ADMIN"},
    )
    session.add(capability)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ManagementCapability(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                advertiser_id=account.advertiser_id,
                connection_id=route.connection_id,
                authorization_revision=route.authorization_revision,
                binding_revision=route.binding_revision,
                adapter_contract_revision=route.adapter_contract_revision,
                operation="ads_manage",
                entity_kind="adgroup",
                state="VERIFIED",
                verified_at=datetime.now(UTC),
                evidence={"role": "ADMIN"},
            )
        )
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ManagementCapability(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                advertiser_id=account.advertiser_id,
                connection_id=route.connection_id,
                authorization_revision=route.authorization_revision,
                binding_revision=route.binding_revision + 1,
                adapter_contract_revision=route.adapter_contract_revision,
                operation="update_budget",
                entity_kind="adgroup",
                state="VERIFIED",
                verified_at=datetime.now(UTC),
                evidence={"role": "ADMIN"},
            )
        )
        session.flush()

    preview = ManagementPreview(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        actor_id=context.actor_id,
        selection_id=selection.id,
        digest="p" * 64,
        route=route.model_dump(mode="json"),
        mutation={},
        counts={},
    )
    session.add(preview)
    session.flush()
    preview_item = ManagementPreviewItem(
        tenant_id=context.tenant_id,
        preview_id=preview.id,
        ref={"kind": "adgroup", "remote_id": "g1"},
    )
    session.add(preview_item)
    session.flush()
    task = ManagementTask(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        actor_id=context.actor_id,
        preview_id=preview.id,
        idempotency_key=uuid4(),
        digest="p" * 64,
        route=route.model_dump(mode="json"),
        counts={},
    )
    session.add(task)
    session.flush()
    other_preview = ManagementPreview(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        actor_id=context.actor_id,
        selection_id=selection.id,
        digest="q" * 64,
        route=route.model_dump(mode="json"),
        mutation={},
        counts={},
    )
    session.add(other_preview)
    session.flush()
    other_preview_item = ManagementPreviewItem(
        tenant_id=context.tenant_id,
        preview_id=other_preview.id,
        ref={"kind": "adgroup", "remote_id": "g2"},
    )
    session.add(other_preview_item)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ManagementTaskItem(
                tenant_id=context.tenant_id,
                task_id=task.id,
                preview_item_id=other_preview_item.id,
                preview_id=preview.id,
                ref={"kind": "adgroup", "remote_id": "g2"},
            )
        )
        session.flush()
    item = ManagementTaskItem(
        tenant_id=context.tenant_id,
        task_id=task.id,
        preview_item_id=preview_item.id,
        preview_id=preview.id,
        ref={"kind": "adgroup", "remote_id": "g1"},
    )
    session.add(item)
    session.flush()
    session.add(
        ManagementRequestAttempt(
            tenant_id=context.tenant_id, task_item_id=item.id, attempt=1
        )
    )
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ManagementRequestAttempt(
                tenant_id=context.tenant_id, task_item_id=item.id, attempt=1
            )
        )
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            ManagementRequestAttempt(
                tenant_id=context.tenant_id, task_item_id=item.id, attempt=0
            )
        )
        session.flush()


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
    if change == "binding":
        with pytest.raises(IntegrityError), session.begin_nested():
            session.flush()
        return
    session.flush()
    with pytest.raises(DomainError):
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=account.advertiser_id,
            capability="ads_manage",
            operation="update_roas",
            entity_kind="adgroup",
        )
    assert not session.exec(
        usable_grants(tenant_id=context.tenant_id, bc_id=bc.bc_id, action="ads_manage")
    ).all()
