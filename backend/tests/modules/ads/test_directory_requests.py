from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.modules.ads.directory_requests import (
    AD_TYPE_PROBE_INTERVAL,
    DIRECTORY_AD_TYPES,
    INACTIVE_DIRECTORY_INTERVAL,
    KINDS_BY_TYPE,
    directory_ad_types,
    directory_due_accounts,
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


PROBES = dict.fromkeys(DIRECTORY_AD_TYPES, ("campaign",))


def test_unused_ad_types_are_probed_once_per_day(session, sync_seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    args = {"tenant_id": sync_seed.tenant_id, "advertiser_ids": ["acc-1", "acc-2"]}

    # 首次：没有任何目录与探测记录，各类型只探测广告系列。
    first = directory_ad_types(session, **args, active_ids=[], now=now)
    assert first == {"acc-1": PROBES, "acc-2": PROBES}
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
    assert created == 6
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {r.kind for r in runs} == {"campaign"}

    # 探测完成后：acc-1 只在用 Smart+（全部层级），acc-2 没有任何广告。
    _complete_probes(session, sync_seed, "acc-1", completed_at=now)
    _complete_probes(session, sync_seed, "acc-2", completed_at=now)
    session.add(_ad_object(sync_seed, "acc-1", "SMART_PLUS"))
    session.flush()
    later = now + timedelta(hours=3)
    assert directory_ad_types(session, **args, active_ids=[], now=later) == {
        "acc-1": {"SMART_PLUS": KINDS_BY_TYPE["SMART_PLUS"]},
        "acc-2": {},
    }

    # 有消耗但目录还没有任何对象的账户，不等复查立即请求全部类型全部层级。
    assert (
        directory_ad_types(session, **args, active_ids=["acc-2"], now=later)["acc-2"]
        == KINDS_BY_TYPE
    )

    # 超过 24 小时，未使用类型重新探测一次广告系列。
    stale = now + AD_TYPE_PROBE_INTERVAL + timedelta(minutes=1)
    assert directory_ad_types(session, **args, active_ids=[], now=stale) == {
        "acc-1": {**PROBES, "SMART_PLUS": KINDS_BY_TYPE["SMART_PLUS"]},
        "acc-2": PROBES,
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
        ad_types={"acc-1": {"SMART_PLUS": KINDS_BY_TYPE["SMART_PLUS"]}, "acc-2": {}},
    )
    # Smart+ 的广告系列、广告组、广告与创意共 4 个；空账户本轮不请求。
    assert created == 4
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {(r.advertiser_id, r.ad_type) for r in runs} == {("acc-1", "SMART_PLUS")}


def test_queued_run_absorbs_later_identical_requests(session, sync_seed, monkeypatch):
    """积压期间后续轮次的相同查询并入尚未开始的运行，不叠加；已领取后才新建。"""
    from uuid import uuid4

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    base = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "scope": "directory",
        "ad_types": {
            "acc-1": {"SMART_PLUS": ("campaign",)},
            "acc-2": {"SMART_PLUS": ("campaign",)},
        },
    }
    assert request_directory(session, advertiser_ids=["acc-1"], **base) == 1
    # 账户集合不同即是另一次请求；acc-1 的相同查询并入排队中的运行。
    assert request_directory(session, advertiser_ids=["acc-1", "acc-2"], **base) == 1
    queued = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == sync_seed.tenant_id,
            AdDirectoryRun.advertiser_id == "acc-1",
        )
    ).one()
    queued.claim_token = uuid4()
    session.flush()
    # 已被领取的运行可能已读过首页，不能代替新请求，因此 acc-1 新建一条。
    assert request_directory(session, advertiser_ids=["acc-2", "acc-1"], **base) == 1


def test_targeted_read_is_queued_immediately_and_completed_run_is_never_reused(
    session, sync_seed, monkeypatch
):
    from app.integrations.tiktok.contracts.ads import EntityRef
    from app.jobs.models import PendingDispatch
    from app.jobs.tasks import dispatch_queue

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "advertiser_ids": ["acc-1"],
        "scope": "targeted",
        "refs": [EntityRef(sync_seed.tenant_id, "acc-1", "ad", "changed-ad")],
        "ad_types": {"acc-1": {"SMART_PLUS": ("ad",)}},
    }
    assert request_directory(session, **args) == 1
    run = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == sync_seed.tenant_id,
        )
    ).one()
    dispatch = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
        )
    ).one()
    assert dispatch.task_name == "ads.targeted_step"
    assert dispatch_queue(dispatch.task_name) == "ads-live-directory"
    run.status, run.completed_at = "COMPLETE", datetime.now(UTC)
    session.flush()
    # 同一秒再次启停也必须读取新的平台状态；完成的旧结果不作为新操作回读。
    assert request_directory(session, **args) == 1


def test_idle_accounts_refresh_daily(session, sync_seed):
    now = datetime.now(UTC)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "advertiser_ids": ["acc-1", "acc-2"],
    }
    # 从未成功过：全部需要刷新。
    assert directory_due_accounts(session, **args, active_ids=[], now=now) == [
        "acc-1",
        "acc-2",
    ]
    session.add(
        AdDirectoryRun(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-2",
            bc_id="bc-a",
            actor_id=sync_seed.actor_id,
            connection_id=sync_seed.connection.id,
            channel="OFFICIAL_API",
            frozen_route=_route(sync_seed),
            partition_key="a" * 64,
            query={"include_deleted": True},
            status="COMPLETE",
            claim_generation=1,
            completed_at=now - timedelta(hours=1),
            kind="campaign",
            ad_type="SMART_PLUS",
        )
    )
    session.flush()
    # acc-2 一小时前刚完成且无消耗：本轮跳过；有消耗的账户每轮都刷新。
    assert directory_due_accounts(session, **args, active_ids=[], now=now) == ["acc-1"]
    assert directory_due_accounts(session, **args, active_ids=["acc-2"], now=now) == [
        "acc-1",
        "acc-2",
    ]
    later = now + INACTIVE_DIRECTORY_INTERVAL
    assert directory_due_accounts(session, **args, active_ids=[], now=later) == [
        "acc-1",
        "acc-2",
    ]


@pytest.mark.parametrize(
    "status", ["QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE"]
)
def test_pending_full_directory_excludes_overlapping_accounts(
    session, sync_seed, monkeypatch, status
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "advertiser_ids": ["acc-1"],
    }
    request_directory(session, **args, scope="directory")
    runs = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all()
    for run in runs:
        run.status = status
    session.flush()
    args["advertiser_ids"] = ["acc-1", "acc-2"]
    assert directory_due_accounts(
        session, **args, active_ids=["acc-1", "acc-2"], now=datetime.now(UTC)
    ) == ["acc-2"]
    # 冻结授权变化后的新任务不能被旧授权积压遮挡。
    args["frozen_route"] = {**args["frozen_route"], "binding_revision": 1}
    assert directory_due_accounts(
        session, **args, active_ids=["acc-1", "acc-2"], now=datetime.now(UTC)
    ) == ["acc-1", "acc-2"]


@pytest.mark.parametrize(
    "scope,status",
    [
        ("targeted", "RUNNING"),
        ("directory", "COMPLETE"),
        ("directory", "FAILED"),
        ("directory", "CANCELLED"),
        ("directory", "STALE"),
    ],
)
def test_targeted_and_terminal_directory_do_not_block_active_refresh(
    session, sync_seed, monkeypatch, scope, status
):
    from app.integrations.tiktok.contracts.ads import EntityRef

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "actor_id": sync_seed.actor_id,
        "frozen_route": _route(sync_seed),
        "advertiser_ids": ["acc-1"],
    }
    request_directory(
        session,
        **args,
        scope=scope,
        refs=[EntityRef(sync_seed.tenant_id, "acc-1", "ad", "ad-1")]
        if scope == "targeted"
        else (),
    )
    for run in session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).all():
        run.status = status
    session.flush()
    assert directory_due_accounts(
        session, **args, active_ids=["acc-1"], now=datetime.now(UTC)
    ) == ["acc-1"]
