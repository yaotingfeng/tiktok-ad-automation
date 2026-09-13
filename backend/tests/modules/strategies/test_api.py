from datetime import timedelta
from uuid import uuid4

from app.core.security import create_access_token
from app.modules.strategies.copy_pool import POOL_VERSION
from app.modules.tenants.models import TenantMembership
from tests.modules.strategies.test_versions import config


def headers(context):
    return {
        "Authorization": "Bearer "
        + create_access_token(context.actor_id, timedelta(minutes=5))
    }


def test_create_retry_returns_its_saved_version_after_a_later_edit(client, context):
    base = f"/api/tenants/{context.tenant_id}"
    request = uuid4()
    body = {
        "name": "Original",
        "config": config().model_dump(mode="json"),
        "request_id": str(request),
    }
    first = client.post(f"{base}/strategies", json=body, headers=headers(context))
    saved = client.get(
        f"{base}/strategy-save-requests/{request}", headers=headers(context)
    ).json()
    appended = client.post(
        f"{base}/strategies/{saved['strategy_id']}/versions",
        json={
            "config": config(budget="200").model_dump(mode="json"),
            "request_id": str(uuid4()),
            "expected_version": 1,
        },
        headers=headers(context),
    )
    assert appended.status_code == 201
    replay = client.post(f"{base}/strategies", json=body, headers=headers(context))
    assert replay.status_code == 201
    assert replay.json() == first.json()


def test_versioned_save_recovery_history_and_local_disable(client, context):
    base = f"/api/tenants/{context.tenant_id}"
    request = uuid4()
    body = {
        "name": "普通短剧",
        "config": config().model_dump(mode="json"),
        "request_id": str(request),
    }
    first = client.post(f"{base}/strategies", json=body, headers=headers(context))
    assert first.status_code == 201, first.text
    initial = first.json()
    assert initial["config"]["budget"] == "100.25"
    assert initial["number"] == 1
    assert (
        client.post(f"{base}/strategies", json=body, headers=headers(context)).json()[
            "id"
        ]
        == initial["id"]
    )
    recovered = client.get(
        f"{base}/strategy-save-requests/{request}", headers=headers(context)
    )
    assert recovered.json()["id"] == initial["id"]
    revised = client.post(
        f"{base}/strategies/{initial['strategy_id']}/versions",
        json={
            "config": config(budget="200").model_dump(mode="json"),
            "request_id": str(uuid4()),
            "expected_version": 1,
        },
        headers=headers(context),
    )
    assert revised.status_code == 201, revised.text
    assert revised.json()["number"] == 2
    assert (
        client.get(
            f"{base}/strategy-versions/{initial['id']}",
            headers=headers(context),
        ).json()["config"]["budget"]
        == "100.25"
    )
    history = client.get(
        f"{base}/strategies/{initial['strategy_id']}/versions?limit=1", headers=headers(context)
    ).json()
    assert history["items"][0]["number"] == 2
    assert (
        client.get(
            f"{base}/strategies/{initial['strategy_id']}/versions",
            params={"limit": 1, "cursor": history["next_cursor"]},
            headers=headers(context),
        ).json()["items"][0]["number"]
        == 1
    )
    assert (
        client.patch(
            f"{base}/strategies/{initial['strategy_id']}",
            json={"active": False},
            headers=headers(context),
        ).json()["active"]
        is False
    )
    assert (
        client.get(f"{base}/strategies?active=true", headers=headers(context)).json()[
            "items"
        ]
        == []
    )
    assert (
        client.get(f"{base}/strategies?active=false", headers=headers(context)).json()[
            "items"
        ][0]["id"]
        == initial["strategy_id"]
    )


def test_copy_pool_and_aggregated_local_validation(client, context):
    base = f"/api/tenants/{context.tenant_id}"
    pool = client.get(f"{base}/copy-pools/{POOL_VERSION}", headers=headers(context))
    assert pool.status_code == 200
    assert len(pool.json()["entries"]) == 100
    invalid = config(creative_count=101, campaign_name_template="-{unsafe}").model_dump(
        mode="json"
    )
    checked = client.post(
        f"{base}/strategies/validate", json=invalid, headers=headers(context)
    )
    assert checked.status_code == 200
    assert checked.json()["valid"] is False
    assert {item["field"] for item in checked.json()["errors"]} == {
        "creative_count",
        "campaign_name_template",
    }
    assert checked.json()["scene_check_pending"] is True
    rejected = client.post(
        f"{base}/strategies",
        json={"name": "S", "config": invalid, "request_id": str(uuid4())},
        headers=headers(context),
    )
    assert rejected.status_code == 422


def test_api_scope_role_and_strict_fields(client, session, context, other_context):
    base = f"/api/tenants/{context.tenant_id}"
    body = {
        "name": "S",
        "config": config().model_dump(mode="json"),
        "request_id": str(uuid4()),
    }
    created = client.post(
        f"{base}/strategies", json=body, headers=headers(context)
    ).json()
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/strategy-versions/{created['id']}",
            headers=headers(other_context),
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{base}/strategies",
            json=body | {"account_pool": []},
            headers=headers(context),
        ).status_code
        == 422
    )
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "viewer"
    session.add(member)
    session.flush()
    assert client.get(f"{base}/strategies", headers=headers(context)).status_code == 200
    assert (
        client.post(
            f"{base}/strategies", json=body, headers=headers(context)
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"{base}/strategies/{created['strategy_id']}",
            json={"active": False},
            headers=headers(context),
        ).status_code
        == 403
    )
