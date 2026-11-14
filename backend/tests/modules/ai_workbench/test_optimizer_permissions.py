import pytest
from sqlmodel import select

from app.modules.tenants.permissions import PERMISSIONS
from tests.modules.conftest import create_context

from .test_optimizer import api_headers, create_body, seed_optimizer_dependencies


@pytest.fixture
def viewer_context(session):
    return create_context(session, role="viewer")


def test_viewer_is_read_only_for_optimizer_configuration():
    assert PERMISSIONS["viewer"] == frozenset({"read"})
    assert "strategy_write" not in PERMISSIONS["viewer"]


def test_operator_can_manage_strategy_scoped_optimizer_configuration():
    assert "strategy_write" in PERMISSIONS["operator"]


def test_viewer_write_is_rejected(client, session, viewer_context):
    strategy = seed_optimizer_dependencies(session, viewer_context)
    response = client.post(
        f"/api/tenants/{viewer_context.tenant_id}/ai-optimizers",
        headers=api_headers(viewer_context),
        json=create_body(strategy.id),
    )
    assert response.status_code == 403


def test_invalid_optimization_mode_is_rejected(client, session, context):
    strategy = seed_optimizer_dependencies(session, context)
    response = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context, session=session),
        json=create_body(strategy.id, optimization_mode="INVALID"),
    )
    assert response.status_code == 422


def test_stale_or_unauthorized_account_scope_is_rejected(client, session, context):
    strategy = seed_optimizer_dependencies(session, context)
    # The canonical usable-grants query rejects stale authorization evidence.
    from app.modules.accounts.models import BCAccountAccess

    grant = session.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == context.tenant_id,
            BCAccountAccess.bc_id == "bc-a",
            BCAccountAccess.advertiser_id == "acc-1",
        )
    ).one()
    assert grant is not None
    grant.authorized = False
    session.flush()
    response = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context, session=session),
        json=create_body(strategy.id),
    )
    assert response.status_code == 422


@pytest.mark.parametrize("role", ["viewer", "operator", "tenant_admin"])
def test_workbench_http_is_closed_to_non_platform_users(client, session, role):
    from uuid import uuid4

    context = create_context(session, role=role)
    base = f"/api/tenants/{context.tenant_id}"
    for path in (
        "/ai-optimizers",
        "/ai-knowledge",
        "/ai-material-options",
        f"/ai-optimizers/{uuid4()}/reviews",
    ):
        response = client.get(base + path, headers=api_headers(context))
        assert response.status_code == 403


def test_platform_admin_can_access_workbench_http(client, session, context):
    from app.models import User

    session.get(User, context.actor_id).is_superuser = True
    session.flush()
    response = client.get(
        f"/api/tenants/{context.tenant_id}/ai-optimizers", headers=api_headers(context)
    )
    assert response.status_code == 200
