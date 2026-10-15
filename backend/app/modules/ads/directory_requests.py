"""广告目录请求独立入队；冻结路由及每半小时槽幂等，发送由目录 Worker 完成。"""

import hashlib
import json
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import func, text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.routing import verify_route
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryRun

DIRECTORY_AD_TYPES = ("REGULAR", "LEGACY_SMART_PLUS", "SMART_PLUS")
# 每个目录层级与广告类型一个运行：(层级, 广告类型, 每页条数)。
DIRECTORY_SPECS = (
    ("campaign", "REGULAR", 1000),
    ("campaign", "LEGACY_SMART_PLUS", 1000),
    ("campaign", "SMART_PLUS", 1000),
    ("adgroup", "REGULAR", 1000),
    ("adgroup", "LEGACY_SMART_PLUS", 1000),
    ("adgroup", "SMART_PLUS", 1000),
    ("ad", "REGULAR", 100),
    ("ad", "LEGACY_SMART_PLUS", 100),
    ("ad", "SMART_PLUS", 100),
    ("creative", "SMART_PLUS", 100),
)
KINDS_BY_TYPE = {
    ad_type: tuple(
        kind for kind, spec_type, _ in DIRECTORY_SPECS if spec_type == ad_type
    )
    for ad_type in DIRECTORY_AD_TYPES
}
# 未使用类型的探测只读广告系列：该类型没有系列就不可能有广告组和广告。
PROBE_KINDS = ("campaign",)
# 账户未使用的广告类型只按此间隔复查，避免每轮都发必然为空的目录请求。
AD_TYPE_PROBE_INTERVAL = timedelta(hours=24)
# 最近无消耗的账户目录基本不变，周期全量只按此间隔刷新；本系统改动另走定向刷新。
INACTIVE_DIRECTORY_INTERVAL = timedelta(hours=24)


def _partition_key(query: dict) -> str:
    return hashlib.sha256(
        json.dumps(query, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _probe_partition(advertiser_id: str, ad_type: str) -> str:
    """全量目录（含已删除）中该类型广告系列首页的分区键，用作类型探测记录。"""
    return _partition_key(
        {
            "advertiser_id": advertiser_id,
            "kind": "campaign",
            "ad_type": ad_type,
            "page": 1,
            "page_size": 1000,
            "ids": [],
            "parent_ids": [],
            "include_deleted": True,
        }
    )


def directory_ad_types(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_ids: Collection[str],
    active_ids: Collection[str],
    now: datetime,
) -> dict[str, dict[str, tuple[str, ...]]]:
    """周期目录按账户实际使用的广告类型裁剪请求，返回 账户 -> 类型 -> 层级。

    业务规则：
    - 账户目录里已有某类型对象（含已删除），该类型全部层级每轮都同步；
    - 未使用的类型，距最近一次成功探测超过 24 小时才只读一次广告系列，
      覆盖账户新启用某类型的情况；没有任何广告的账户因此自然降为每天一次；
    - 最近有消耗但目录里还没有任何对象的账户，全部类型全部层级立即请求，不等复查。
    本系统自己创建或修改的对象另走定向刷新，不受此裁剪影响。
    """
    accounts = sorted(set(advertiser_ids))
    if not accounts:
        return {}
    used: dict[str, set[str]] = {}
    for advertiser_id, ad_type in session.exec(
        select(AdObject.advertiser_id, AdObject.ad_type)
        .where(
            AdObject.tenant_id == tenant_id,
            col(AdObject.advertiser_id).in_(accounts),
        )
        .distinct()
    ):
        used.setdefault(advertiser_id, set()).add(ad_type)
    probes = {
        _probe_partition(advertiser_id, ad_type): (advertiser_id, ad_type)
        for advertiser_id in accounts
        for ad_type in DIRECTORY_AD_TYPES
    }
    probed: set[tuple[str, str]] = set()
    for partition_key in session.exec(
        select(AdDirectoryRun.partition_key)
        .where(
            AdDirectoryRun.tenant_id == tenant_id,
            col(AdDirectoryRun.advertiser_id).in_(accounts),
            col(AdDirectoryRun.partition_key).in_(list(probes)),
            AdDirectoryRun.status == "COMPLETE",
        )
        .group_by(AdDirectoryRun.partition_key)
        .having(func.max(AdDirectoryRun.completed_at) > now - AD_TYPE_PROBE_INTERVAL)
    ):
        probed.add(probes[partition_key])
    active = set(active_ids)
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for advertiser_id in accounts:
        in_use = used.get(advertiser_id, set())
        if advertiser_id in active and not in_use:
            result[advertiser_id] = dict(KINDS_BY_TYPE)
            continue
        result[advertiser_id] = {
            ad_type: KINDS_BY_TYPE[ad_type] if ad_type in in_use else PROBE_KINDS
            for ad_type in DIRECTORY_AD_TYPES
            if ad_type in in_use or (advertiser_id, ad_type) not in probed
        }
    return result


def directory_due_accounts(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_ids: Collection[str],
    active_ids: Collection[str],
    now: datetime,
) -> list[str]:
    """本轮周期目录需要刷新的账户。

    最近有消耗的账户每轮都刷新；其余账户距最近一次成功的周期全量超过
    INACTIVE_DIRECTORY_INTERVAL 才刷新，从未成功过的账户立即刷新。
    """
    accounts = sorted(set(advertiser_ids))
    active = set(active_ids) & set(accounts)
    idle = [advertiser_id for advertiser_id in accounts if advertiser_id not in active]
    fresh: set[str] = set()
    if idle:
        fresh = set(
            session.exec(
                select(AdDirectoryRun.advertiser_id)
                .where(
                    AdDirectoryRun.tenant_id == tenant_id,
                    col(AdDirectoryRun.advertiser_id).in_(idle),
                    AdDirectoryRun.status == "COMPLETE",
                    AdDirectoryRun.query["include_deleted"].as_boolean().is_(True),
                    col(AdDirectoryRun.completed_at)
                    > now - INACTIVE_DIRECTORY_INTERVAL,
                )
                .distinct()
            ).all()
        )
    return [
        advertiser_id
        for advertiser_id in accounts
        if advertiser_id in active or advertiser_id not in fresh
    ]


def directory_id_chunks(
    ids: tuple[str, ...], *, limit: int = 100
) -> tuple[tuple[str, ...], ...]:
    """Split directory filters to the platform's maximum 100 IDs per request."""

    if limit != 100:
        raise ValueError("directory ID limit must remain 100")
    if not ids:
        return ((),)
    return tuple(ids[offset : offset + limit] for offset in range(0, len(ids), limit))


def directory_filter_chunks(
    ids: tuple[str, ...], parent_ids: tuple[str, ...]
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Return a bounded product when both ID filters need pagination."""

    return tuple(
        (id_chunk, parent_chunk)
        for id_chunk in directory_id_chunks(ids)
        for parent_chunk in directory_id_chunks(parent_ids)
    )


def request_directory(
    session: Session,
    *,
    tenant_id,
    bc_id,
    frozen_route,
    actor_id,
    advertiser_ids,
    scope,
    refs=(),
    ad_types: Mapping[str, Mapping[str, Collection[str]]] | None = None,
) -> int:
    """ad_types 为空表示请求全部类型全部层级；周期调度传入 directory_ad_types 的裁剪结果。

    同一账户同一查询已有尚未开始的运行时不再新建：积压期间每轮的周期请求
    不会层层叠加，排队中的那一条开始执行时自然读到最新数据。
    """
    if not settings.ADS_SYNC_ENABLED or frozen_route.get("channel") != "OFFICIAL_API":
        return 0
    route = FrozenTikTokRoute.model_validate(frozen_route)
    advertiser_ids = tuple(sorted(set(advertiser_ids)))
    if (
        route.tenant_id != tenant_id
        or route.bc_id != bc_id
        or scope not in ("directory", "targeted")
    ):
        raise DomainError("directory_request_invalid", "目录请求范围无效")
    if any(
        ref.tenant_id != tenant_id or ref.advertiser_id not in advertiser_ids
        for ref in refs
    ):
        raise DomainError("directory_request_invalid", "目录对象超出账户范围")
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    for advertiser in advertiser_ids:
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=advertiser,
            capability="read",
        )
    now = datetime.now(UTC)
    payload = [
        frozen_route,
        advertiser_ids,
        scope,
        sorted((str(r.tenant_id), r.advertiser_id, r.kind, r.remote_id) for r in refs),
        int(now.timestamp()) // 1800,
    ]
    request_id = uuid5(
        UUID("c5c96c13-7e3a-4cbb-a7cc-9b12e9d1c6ad"),
        json.dumps(payload, sort_keys=True),
    )
    # 同一槽并发请求串行检查，避免重复插入目录任务。
    session.connection().execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": request_id.int % (2**63)}
    )
    created = 0
    for advertiser_id in advertiser_ids:
        account_refs = tuple(ref for ref in refs if ref.advertiser_id == advertiser_id)
        for kind, ad_type, page_size in DIRECTORY_SPECS:
            if ad_types is not None and kind not in ad_types.get(advertiser_id, {}).get(
                ad_type, ()
            ):
                continue
            ids = tuple(
                sorted(
                    {
                        ref.remote_id
                        for ref in account_refs
                        if ref.kind == kind
                        and (
                            scope != "targeted"
                            or (kind == "creative" and ref.kind == "creative")
                            or (kind != "creative" and ref.kind == kind)
                        )
                    }
                )
            )
            parent_kind = {
                "campaign": (),
                "adgroup": ("campaign",),
                "ad": ("adgroup",),
                "creative": ("ad",),
            }[kind]
            parent_ids = tuple(
                sorted(
                    {ref.remote_id for ref in account_refs if ref.kind in parent_kind}
                )
            )
            # TikTok 的创意读取只接受创意 ID，父级广告 ID 不能作为筛选器。
            if kind == "creative":
                parent_ids = ()
            if scope == "targeted" and not ids and not parent_ids:
                continue
            # TikTok 对 filtering.ids/parent_ids 都限制最多 100 个；历史上把全部
            # Smart+ 创意 ID 放进一个运行，导致任务进入队列后必然 ads_query_invalid。
            for id_chunk, parent_chunk in directory_filter_chunks(ids, parent_ids):
                query = {
                    "advertiser_id": advertiser_id,
                    "kind": kind,
                    "ad_type": ad_type,
                    "page": 1,
                    "page_size": page_size,
                    "ids": list(id_chunk),
                    "parent_ids": list(parent_chunk),
                    "include_deleted": scope == "directory",
                }
                partition = _partition_key(query)
                existing = session.exec(
                    select(AdDirectoryRun.id)
                    .where(
                        AdDirectoryRun.tenant_id == tenant_id,
                        AdDirectoryRun.advertiser_id == advertiser_id,
                        AdDirectoryRun.partition_key == partition,
                        # 已被领取的运行可能已读过首页，不能代替本次刷新。
                        (col(AdDirectoryRun.request_id) == request_id)
                        | (
                            (col(AdDirectoryRun.status) == "QUEUED")
                            & col(AdDirectoryRun.claim_token).is_(None)
                        ),
                    )
                    .limit(1)
                ).first()
                if existing is not None:
                    continue
                run = AdDirectoryRun(
                    tenant_id=tenant_id,
                    advertiser_id=advertiser_id,
                    bc_id=route.bc_id,
                    actor_id=actor_id,
                    connection_id=route.connection_id,
                    channel=route.channel,
                    frozen_route=frozen_route,
                    request_id=request_id,
                    partition_key=partition,
                    query=query,
                    status="QUEUED",
                    claim_generation=1,
                    next_page=1,
                    coverage="PENDING",
                    observed_at=now,
                    kind=kind,
                    ad_type=ad_type,
                )
                session.add(run)
                created += 1
    session.flush()
    return created
