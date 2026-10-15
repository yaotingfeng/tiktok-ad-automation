from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.deps import get_current_user
from app.integrations.tiktok.contracts.ads import EntityRef
from app.main import app
from app.models import User
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess, TenantBC
from app.modules.ad_management.rename import resolve_rename_rows
from app.modules.ad_management.schemas import RenameRowInput, RenameSelectionRequest
from tests.modules.ad_management.conftest import _row


def _request(bc, rows):
    return RenameSelectionRequest(
        bc_id=bc.bc_id,
        rows=[RenameRowInput(campaign_id=c, new_name=n) for c, n in rows],
    )


def test_resolve_finds_campaigns_and_reports_problems(session, management_env):
    context, bc, account, route, _ = management_env
    ref = EntityRef(
        context.tenant_id, account.advertiser_id, "campaign", "1700000000000000001"
    )
    session.add(_row(ref, connection_id=route.connection_id))
    session.flush()
    result = resolve_rename_rows(
        session,
        context,
        _request(
            bc,
            [
                ("1700000000000000001", "新名"),
                ("1700000000000000001", "又一个"),
                ("1799999999999999999", "x"),
            ],
        ),
    )
    assert [r.remote_id for r in result.selection.refs] == ["1700000000000000001"]
    assert {(p.campaign_id, p.reason) for p in result.problems} == {
        ("1700000000000000001", "duplicate_campaign_id"),
        ("1799999999999999999", "not_found"),
    }


def test_resolve_rejects_scientific_notation_id(session, management_env):
    context, bc, *_ = management_env
    result = resolve_rename_rows(session, context, _request(bc, [("1.7E+18", "x")]))
    assert result.selection is None
    assert result.problems[0].reason == "id_invalid"


def test_resolve_does_not_cross_bc(session, management_env):
    """其他 BC 的账户下的推广系列即使 ID 正确也判为找不到。"""
    context, bc, _account, route, _ = management_env
    other_bc = TenantBC(tenant_id=context.tenant_id, bc_id="other-bc")
    other_account = AdvertiserAccount(
        tenant_id=context.tenant_id,
        advertiser_id="account-in-other-bc",
        currency="USD",
        timezone="UTC",
        remote_status="STATUS_ENABLE",
    )
    session.add_all([other_bc, other_account])
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=context.tenant_id,
            bc_id=other_bc.bc_id,
            advertiser_id=other_account.advertiser_id,
            connection_id=route.connection_id,
            in_bc=True,
            authorized=True,
            active=True,
            permission_state="VERIFIED",
            checked_at=datetime.now(UTC),
        )
    )
    other = EntityRef(
        context.tenant_id,
        other_account.advertiser_id,
        "campaign",
        "1700000000000000002",
    )
    session.add(_row(other, connection_id=route.connection_id))
    session.flush()
    result = resolve_rename_rows(
        session, context, _request(bc, [("1700000000000000002", "x")])
    )
    assert result.problems[0].reason == "not_found"


def test_resolve_route_returns_selection_and_problems(
    session, management_env, client: TestClient
):
    context, bc, account, route, _ = management_env
    ref = EntityRef(
        context.tenant_id, account.advertiser_id, "campaign", "1700000000000000003"
    )
    session.add(_row(ref, connection_id=route.connection_id))
    session.flush()
    actor = session.get(User, context.actor_id)
    assert actor is not None
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: actor
    try:
        response = client.post(
            f"/api/tenants/{context.tenant_id}/ad-management-rename-selections",
            json={
                "bc_id": bc.bc_id,
                "rows": [
                    {"campaign_id": " 1700000000000000003 ", "new_name": "新名"},
                    {"campaign_id": "abc", "new_name": "x"},
                ],
            },
        )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous
    assert response.status_code == 201
    body = response.json()
    assert [r["remote_id"] for r in body["selection"]["refs"]] == [
        "1700000000000000003"
    ]
    assert body["problems"] == [{"campaign_id": "abc", "reason": "id_invalid"}]


def test_resolve_request_rejects_more_than_max_rows(management_env):
    _context, bc, *_ = management_env
    with pytest.raises(ValidationError):
        _request(bc, [(str(i), "x") for i in range(501)])
