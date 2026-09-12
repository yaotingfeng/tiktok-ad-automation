from app.modules.tenants.models import TenantMembership
from tests.modules.accounts.test_router import headers
from tests.modules.accounts.test_routing import route_case as route_case


def test_default_connection_http_is_admin_only_and_returns_frozen_route(
    client, session, route_case
):
    context, connection, grant, _ = route_case
    path = f"/api/tenants/{context.tenant_id}/bcs/{grant.bc_id}/default-connection"
    body = {"connection_id": str(connection.id)}
    assert client.put(path, headers=headers(context), json=body).status_code == 403
    session.get(
        TenantMembership, (context.tenant_id, context.actor_id)
    ).role = "tenant_admin"
    session.flush()
    response = client.put(path, headers=headers(context), json=body)
    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": str(context.tenant_id),
        "bc_id": grant.bc_id,
        "connection_id": str(connection.id),
        "channel": "OFFICIAL_API",
        "authorization_revision": connection.authorization_revision,
        "binding_revision": 0,
        "adapter_contract_revision": connection.adapter_contract_revision,
    }
    assert (
        client.put(
            path, headers=headers(context), json={**body, "token": "unused"}
        ).status_code
        == 422
    )
