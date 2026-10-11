from sqlmodel import select

from app.core.config import settings
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.sync_models import AdDirectoryRun
from tests.modules.reporting.sync_conftest import sync_seed as sync_seed


def test_full_directory_creates_one_run_per_level_and_is_idempotent(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    route = {
        "tenant_id": str(sync_seed.tenant_id),
        "bc_id": "bc-a",
        "connection_id": str(sync_seed.connection.id),
        "channel": "OFFICIAL_API",
        "authorization_revision": 0,
        "adapter_contract_revision": "official-api-v1",
        "binding_revision": 0,
    }
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "frozen_route": route,
        "actor_id": sync_seed.actor_id,
        "advertiser_ids": ["acc-1"],
        "scope": "directory",
    }
    created = request_directory(session, **args)
    assert created == 10  # campaign/adgroup/ad × 3 类型 + Smart+ creative，与原调度一致
    assert request_directory(session, **args) == 0
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert all(r.query["include_deleted"] for r in runs)


def test_switch_and_mcp_route_never_create_runs(session, sync_seed, monkeypatch):
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "advertiser_ids": ["acc-1"],
        "scope": "directory",
    }
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    assert (
        request_directory(session, frozen_route={"channel": "OFFICIAL_API"}, **args)
        == 0
    )
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    assert (
        request_directory(session, frozen_route={"channel": "OFFICIAL_MCP"}, **args)
        == 0
    )
    assert not session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
