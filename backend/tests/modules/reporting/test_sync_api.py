from datetime import date

import pytest
from sqlmodel import select

from app.api.deps import get_current_user
from app.core.config import settings
from app.models import User
from app.modules.reporting.store_models import ReportSyncJob


@pytest.fixture
def sync_api(client, session, sync_seed):
    previous = client.app.dependency_overrides.get(get_current_user)
    client.app.dependency_overrides[get_current_user] = lambda: session.get(
        User, sync_seed.actor_id
    )
    try:
        yield client, f"/api/tenants/{sync_seed.tenant_id}"
    finally:
        if previous is None:
            client.app.dependency_overrides.pop(get_current_user, None)
        else:
            client.app.dependency_overrides[get_current_user] = previous


def test_disabled_refresh_is_visible_and_queues_nothing(
    sync_api, session, sync_seed, monkeypatch
):
    client, path = sync_api
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    response = client.post(
        f"{path}/ad-sync-refresh", params={"bc_id": "bc-a"}, json={"advertiser_ids": []}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SYNC_DISABLED"
    assert not session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()


def test_refresh_inserts_priority_one_jobs_and_coalesces(
    sync_api, session, sync_seed, monkeypatch
):
    client, path = sync_api
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    response = client.post(
        f"{path}/ad-sync-refresh", params={"bc_id": "bc-a"}, json={"advertiser_ids": []}
    )
    assert response.status_code == 200, response.text
    assert response.json()["queued"] >= 2 and response.json()["accounts"] == 2
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {j.priority for j in jobs} == {1}
    assert {j.kind for j in jobs} == {"ad_day", "campaign_hour", "material_day"}
    repeat = client.post(
        f"{path}/ad-sync-refresh", params={"bc_id": "bc-a"}, json={"advertiser_ids": []}
    )
    assert repeat.json()["queued"] == 0


def test_status_only_returns_current_bc_accounts(sync_api):
    client, path = sync_api
    response = client.get(
        f"{path}/ad-sync-status",
        params={"bc_id": "bc-b", "start_date": "2026-10-05", "end_date": "2026-10-05"},
    )
    assert response.status_code == 200, response.text
    assert [a["advertiser_id"] for a in response.json()["accounts"]] == ["acc-2"]
    assert response.json()["accounts"][0]["incomplete_dates"] == [
        date(2026, 10, 5).isoformat()
    ]


def test_foreign_account_cannot_be_refreshed(sync_api, monkeypatch):
    client, path = sync_api
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    response = client.post(
        f"{path}/ad-sync-refresh",
        params={"bc_id": "bc-b"},
        json={"advertiser_ids": ["acc-1"]},
    )
    assert response.status_code == 403


def test_refresh_without_api_binding_is_visible(
    sync_api, session, sync_seed, monkeypatch
):
    from app.modules.accounts.connection_models import BCConnectionBinding

    client, path = sync_api
    session.delete(
        session.get(
            BCConnectionBinding, (sync_seed.tenant_id, "bc-a", sync_seed.connection.id)
        )
    )
    session.flush()
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    response = client.post(
        f"{path}/ad-sync-refresh", params={"bc_id": "bc-a"}, json={"advertiser_ids": []}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "API_BINDING_REQUIRED"


def test_manual_request_does_not_create_other_tenant_plans(
    sync_api, session, monkeypatch
):
    from app.modules.reporting.store_models import AdSyncBc
    from tests.modules.reporting.sync_conftest import sync_seed as make_seed

    other = make_seed.__wrapped__(session)
    client, path = sync_api
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    response = client.post(
        f"{path}/ad-sync-refresh", params={"bc_id": "bc-a"}, json={"advertiser_ids": []}
    )
    assert response.status_code == 200, response.text
    assert session.get(AdSyncBc, (other.tenant_id, "bc-a")) is None
