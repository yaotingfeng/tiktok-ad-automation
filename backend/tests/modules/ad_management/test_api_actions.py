"""C6 RED coverage for scoped management actions and HTTP contracts."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.api.deps import get_current_user
from app.core.errors import DomainError
from app.main import app
from app.models import User
from app.modules.accounts.management_capability_models import ManagementCapability
from app.modules.ad_management.actions import (
    cancel_task,
    get_task,
    list_tasks,
    prepare_restore,
    reconcile,
    retry_task,
)
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementPreviewItem,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.submissions import submit_management_task
from app.modules.tenants.models import TenantMembership


def _task(session: Session, management_env, *, result="PENDING"):
    context, bc, account, route, selection = management_env
    session.add(
        ManagementCapability(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            advertiser_id=account.advertiser_id,
            connection_id=route.connection_id,
            authorization_revision=route.authorization_revision,
            binding_revision=route.binding_revision,
            adapter_contract_revision=route.adapter_contract_revision,
            operation="update_budget",
            entity_kind="adgroup",
            state="VERIFIED",
            verified_at=datetime.now(UTC),
            evidence={"source": "c6-test"},
        )
    )
    session.flush()
    preview = ManagementPreview(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        actor_id=context.actor_id,
        selection_id=selection.id,
        digest="c6" * 32,
        mutation={"field": "budget", "mode": "set", "value": "25"},
        route=route.model_dump(mode="json"),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        status="READY",
        counts={"selected": 1, "targets": 1, "linked": 0, "unsupported": 0},
    )
    session.add(preview)
    session.flush()
    ref = {
        "tenant_id": str(context.tenant_id),
        "advertiser_id": account.advertiser_id,
        "kind": "adgroup",
        "remote_id": "c6-group",
    }
    preview_item = ManagementPreviewItem(
        tenant_id=context.tenant_id,
        preview_id=preview.id,
        position=0,
        ref=ref,
        original_value="20",
        final_value="25",
        execution_result=result,
        capability={
            "operation": "update_budget",
            "entity_kind": "adgroup",
            "configuration": {"budget": "20"},
            "route": route.model_dump(mode="json"),
        },
    )
    session.add(preview_item)
    session.flush()
    task = ManagementTask(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        actor_id=context.actor_id,
        preview_id=preview.id,
        idempotency_key=uuid4(),
        digest=preview.digest,
        route=route.model_dump(mode="json"),
        status="QUEUED",
        counts=preview.counts,
    )
    session.add(task)
    session.flush()
    item = ManagementTaskItem(
        tenant_id=context.tenant_id,
        task_id=task.id,
        preview_item_id=preview_item.id,
        preview_id=preview.id,
        position=0,
        ref=ref,
        original_value="20",
        final_value="25",
        execution_result=result,
        capability=dict(preview_item.capability),
    )
    session.add(item)
    session.flush()
    return context, task, item


def test_readonly_cancel_and_restore_scope(session, management_env):
    context, task, item = _task(session, management_env)
    public = get_task(session, context, task.bc_id, task.id)
    assert public.task_id == task.id
    assert list_tasks(session, context, task.bc_id, cursor=None, limit=10).items
    cancelled = cancel_task(session, context, task.id)
    assert cancelled.status == "CANCELLED"
    assert session.get(ManagementTaskItem, item.id).execution_result == "CANCELLED"
    with pytest.raises(DomainError):
        cancel_task(session, context, task.id)
    restored = prepare_restore(session, context, task.id)
    assert restored.preview_id != task.preview_id
    assert restored.items[0].execution_result == "CONFLICT"
    submitted = submit_management_task(
        session,
        context,
        restored.preview_id,
        restored.digest,
        uuid4(),
    )
    assert submitted.task_id != task.id


def test_retry_requires_idempotency_and_never_retries_unknown(session, management_env):
    context, task, item = _task(session, management_env, result="UNKNOWN")
    with pytest.raises(DomainError):
        retry_task(session, context, task.id, uuid4())


def test_cross_bc_is_not_visible_and_cancel_does_not_touch_claimed_item(session, management_env):
    context, task, item = _task(session, management_env)
    with pytest.raises(DomainError):
        get_task(session, context, "other-bc", task.id)
    item.claim_token = uuid4()
    session.add(item)
    session.flush()
    cancelled = cancel_task(session, context, task.id)
    assert cancelled.status == "CANCELLED"
    assert session.get(ManagementTaskItem, item.id).execution_result == "PENDING"


def test_viewer_can_query_but_cannot_write(session, management_env):
    context, task, _item = _task(session, management_env)
    membership = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    assert membership is not None
    membership.role = "viewer"
    session.add(membership)
    session.flush()
    assert get_task(session, context, task.bc_id, task.id).task_id == task.id
    with pytest.raises(DomainError):
        cancel_task(session, context, task.id)


def test_retry_idempotency_returns_same_task(session, management_env):
    context, source, item = _task(session, management_env, result="NOT_SENT")
    item.delivery_status = "NOT_SENT"
    session.add(item)
    first_key = uuid4()
    first = retry_task(session, context, source.id, first_key)
    second = retry_task(session, context, source.id, first_key)
    assert second.task_id == first.task_id


def test_reconcile_is_read_only_for_accepted_item(session, management_env):
    context, task, item = _task(session, management_env, result="ACCEPTED")
    before = item.execution_result
    public = reconcile(session, context, task.id)
    assert public.task_id == task.id
    assert session.get(ManagementTaskItem, item.id).execution_result == before


def test_management_http_routes_have_scoped_operation_ids():
    spec = app.openapi()
    routes = {
        (method, path): payload["operationId"]
        for path, methods in spec["paths"].items()
        for method, payload in methods.items()
        if method in {"get", "post"} and "ad-management" in path
    }
    assert len(routes) == 8
    assert all(operation.startswith("ad_management-") for operation in routes.values())


def test_management_http_get_task_uses_tenant_scope(
    session: Session, management_env, client: TestClient
):
    context, task, _item = _task(session, management_env)
    actor = session.get(User, context.actor_id)
    assert actor is not None
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: actor
    try:
        response = client.get(
            f"/api/tenants/{context.tenant_id}/ad-management-tasks/{task.id}"
        )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous
    assert response.status_code == 200
    assert response.json()["task_id"] == str(task.id)
