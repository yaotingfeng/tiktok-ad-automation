from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.core.config import settings
from app.modules.ads.directory_requests import (
    AD_TYPE_PROBE_INTERVAL,
    DIRECTORY_AD_TYPES,
    directory_ad_types,
    request_directory,
)
from app.modules.ads.models import AdObject
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


def _route(seed):
    return {
        "tenant_id": str(seed.tenant_id),
        "bc_id": "bc-a",
        "connection_id": str(seed.connection.id),
        "channel": "OFFICIAL_API",
        "authorization_revision": 0,
        "adapter_contract_revision": "official-api-v1",
        "binding_revision": 0,
    }


def _complete_probes(session, seed, advertiser_id, *, completed_at):
    """模拟一轮全量目录已成功完成，留下每种类型的探测记录。"""
    for run in session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == seed.tenant_id,
            AdDirectoryRun.advertiser_id == advertiser_id,
        )
    ).all():
        run.status, run.completed_at = "COMPLETE", completed_at
        session.add(run)
    session.flush()


def _ad_object(seed, advertiser_id, ad_type):
    return AdObject(
        tenant_id=seed.tenant_id,
        advertiser_id=advertiser_id,
        kind="campaign",
        remote_id=f"c-{advertiser_id}-{ad_type}",
        ad_type=ad_type,
        observed_at=datetime.now(UTC),
        published_version=1,
    )


def test_unused_ad_types_are_probed_once_per_day(session, sync_seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    args = {"tenant_id": sync_seed.tenant_id, "advertiser_ids": ["acc-1", "acc-2"]}

    # 首次：没有任何目录与探测记录，全部类型都要请求。
    first = directory_ad_types(session, **args, active_ids=[], now=now)
    assert first == {"acc-1": DIRECTORY_AD_TYPES, "acc-2": DIRECTORY_AD_TYPES}
    created = request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1", "acc-2"],
        scope="directory",
        ad_types=first,
    )
    assert created == 20

    # 探测完成后：acc-1 只在用 Smart+，acc-2 没有任何广告。
    _complete_probes(session, sync_seed, "acc-1", completed_at=now)
    _complete_probes(session, sync_seed, "acc-2", completed_at=now)
    session.add(_ad_object(sync_seed, "acc-1", "SMART_PLUS"))
    session.flush()
    later = now + timedelta(hours=3)
    assert directory_ad_types(session, **args, active_ids=[], now=later) == {
        "acc-1": ("SMART_PLUS",),
        "acc-2": (),
    }

    # 有消耗但目录还没有任何对象的账户，不等复查立即请求全部类型。
    assert (
        directory_ad_types(session, **args, active_ids=["acc-2"], now=later)["acc-2"]
        == DIRECTORY_AD_TYPES
    )

    # 超过 24 小时，未使用类型重新探测一次。
    stale = now + AD_TYPE_PROBE_INTERVAL + timedelta(minutes=1)
    assert directory_ad_types(session, **args, active_ids=[], now=stale) == {
        "acc-1": DIRECTORY_AD_TYPES,
        "acc-2": DIRECTORY_AD_TYPES,
    }


def test_trimmed_request_only_creates_runs_for_used_types(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    created = request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1", "acc-2"],
        scope="directory",
        ad_types={"acc-1": ("SMART_PLUS",), "acc-2": ()},
    )
    # Smart+ 的广告系列、广告组、广告与创意共 4 个；空账户本轮不请求。
    assert created == 4
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {(r.advertiser_id, r.ad_type) for r in runs} == {("acc-1", "SMART_PLUS")}
