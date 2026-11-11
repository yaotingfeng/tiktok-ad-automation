from sqlmodel import select

from app.modules.tenants.permissions import PERMISSIONS

from .test_optimizer import api_headers, create_body, seed_optimizer_dependencies


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
        headers=api_headers(context),
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
        headers=api_headers(context),
        json=create_body(strategy.id),
    )
    assert response.status_code == 422
