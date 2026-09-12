"""多 BC 管理 HTTP 合同与状态投影；不访问上游。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.accounts import mcp_router
from app.modules.accounts.connection_models import BCConnectionBinding
from app.modules.accounts.models import DiscoveryRun, TenantBC
from app.modules.tenants.models import TenantMembership
from tests.modules.accounts.test_mcp_binding import (
    committed_context as committed_context,
)
from tests.modules.accounts.test_router import headers


@pytest.mark.parametrize(
    "payload",
    [
        {"bc_ids": []},
        {"bc_ids": ["a", "a"]},
        {"bc_ids": [" "]},
        {"bc_ids": [" a"]},
        {"bc_ids": ["a" * 129]},
        {"bc_id": "a"},
    ],
)
def test_binding_rejects_invalid_selection_before_service(
    client, context, monkeypatch, payload
):
    def unexpected(**_kwargs):
        pytest.fail("invalid selection reached management service")

    monkeypatch.setattr(mcp_router, "bind_candidate_bcs", unexpected)
    result = client.post(
        f"/api/tenants/{context.tenant_id}/tiktok/mcp/candidates/{uuid4()}/binding",
        headers=headers(context),
        json=payload,
    )
    assert result.status_code == 422


def test_batch_binding_returns_each_bc_and_one_connection(
    client, committed_context, monkeypatch
):
    context = committed_context
    connection_id, attempt_id = uuid4(), uuid4()
    runs = [
        {"bc_id": bc, "discovery_run_id": uuid4(), "status": "RUNNING"}
        for bc in ("a", "b")
    ]
    calls = []

    def bind(**kwargs):
        calls.append(kwargs)
        return connection_id, runs

    monkeypatch.setattr(mcp_router, "bind_candidate_bcs", bind)
    result = client.post(
        f"/api/tenants/{context.tenant_id}/tiktok/mcp/candidates/{attempt_id}/binding",
        headers=headers(context),
        json={"bc_ids": ["a", "b"]},
    )
    assert result.status_code == 200, result.text
    assert result.json()["connection_id"] == str(connection_id)
    assert [item["bc_id"] for item in result.json()["items"]] == ["a", "b"]
    assert len(calls) == 1 and calls[0]["attempt_id"] == attempt_id
    assert calls[0]["bc_ids"] == ["a", "b"]


def test_available_directory_pagination_refresh_and_no_store(
    client, committed_context, monkeypatch
):
    context = committed_context
    connection_id = uuid4()
    calls = []

    def directory(**kwargs):
        calls.append(kwargs)
        return [
            {
                "bc_id": str(i),
                "name": f"BC {i}",
                "connected": i < 5,
                "binding_status": "ACTIVE" if i < 5 else None,
            }
            for i in range(55)
        ]

    monkeypatch.setattr(mcp_router, "connection_business_centers", directory)
    result = client.get(
        f"/api/tenants/{context.tenant_id}/tiktok/mcp/connections/{connection_id}/available-bcs",
        headers=headers(context),
        params={"page": 2, "page_size": 50, "refresh": True},
    )
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["total"] == 55
    assert [item["bc_id"] for item in result.json()["items"]] == [
        str(i) for i in range(50, 55)
    ]
    assert calls[0]["refresh"] is True and calls[0]["connection_id"] == connection_id


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("get", "available-bcs", None),
        ("post", "bindings", {"bc_ids": ["a"]}),
        ("post", "bcs/a/sync", None),
        ("delete", "bcs/a", None),
    ],
)
def test_operator_cannot_manage_shared_authorization(
    client, context, method, suffix, body
):
    response = client.request(
        method,
        f"/api/tenants/{context.tenant_id}/tiktok/mcp/connections/{uuid4()}/{suffix}",
        headers=headers(context),
        json=body,
    )
    assert response.status_code == 403


def test_sync_refreshes_evidence_before_creating_scoped_run(
    client, committed_context, monkeypatch
):
    calls, run_id = [], uuid4()
    monkeypatch.setattr(
        mcp_router,
        "connection_business_centers",
        lambda **kw: calls.append("directory") or [],
    )
    monkeypatch.setattr(
        mcp_router,
        "sync_connection_bc",
        lambda *args, **kw: calls.append(kw["bc_id"]) or run_id,
    )
    response = client.post(
        f"/api/tenants/{committed_context.tenant_id}/tiktok/mcp/connections/{uuid4()}/bcs/a/sync",
        headers=headers(committed_context),
    )
    assert response.status_code == 200
    assert response.json()["discovery_run_id"] == str(run_id)
    assert calls == ["directory", "a"]


def test_per_bc_status_pending_count_and_unbound_workspace(
    client, session, account_access_case
):
    context, grant = account_access_case
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "tenant_admin"
    binding = session.get(
        BCConnectionBinding, (context.tenant_id, grant.bc_id, grant.connection_id)
    )
    binding.status = "SYNCING"
    session.add_all(
        [
            TenantBC(tenant_id=context.tenant_id, bc_id=bc)
            for bc in ("error-bc", "removed-bc")
        ]
    )
    session.flush()
    for bc, status in (("error-bc", "ERROR"), ("removed-bc", "DISABLED")):
        session.add(
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id=bc,
                connection_id=grant.connection_id,
                kind="OFFICIAL_API",
                status=status,
                last_error_code="discovery_failed" if status == "ERROR" else None,
            )
        )
    session.add(
        DiscoveryRun(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            connection_id=grant.connection_id,
            bc_id="error-bc",
            status="ERROR",
            error_code="discovery_failed",
            completed_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    session.flush()
    root = f"/api/tenants/{context.tenant_id}"
    result = client.get(root + "/tiktok/connections", headers=headers(context))
    item = next(
        item
        for item in result.json()["items"]
        if item["id"] == str(grant.connection_id)
    )
    assert item["binding_count"] == 2 and item["pending_binding_count"] == 1
    result = client.get(
        root + "/bcs",
        headers=headers(context),
        params={"connection_id": str(grant.connection_id)},
    )
    rows = {item["bc_id"]: item for item in result.json()["items"]}
    assert set(rows) == {grant.bc_id, "error-bc"}
    assert rows[grant.bc_id]["binding_status"] == "SYNCING"
    assert rows["error-bc"]["discovery_status"] == "ERROR"
    assert rows["error-bc"]["error_code"] == "discovery_failed"
    result = client.get(root + "/bcs", headers=headers(context))
    assert "removed-bc" not in {item["bc_id"] for item in result.json()["items"]}
