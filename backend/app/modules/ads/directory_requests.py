"""目录按半小时槽合并；操作回读单独投递，保留冻结路由与逐次写后的新观察。"""

import hashlib
import json
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import and_, text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.jobs.outbox import enqueue_after_commit
from app.modules.accounts.routing import verify_route
from app.modules.ads.directory_dispatch import enqueue_directory_step
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
# 广告整页读取会补齐同组创意与素材关联；周期目录不再重复扫全账户创意。
# 显式目录请求仍保留 DIRECTORY_SPECS 的完整能力。
_PERIODIC_KINDS_BY_TYPE = {
    ad_type: tuple(kind for kind in kinds if kind != "creative")
    for ad_type, kinds in KINDS_BY_TYPE.items()
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
    session.flush()
    used: dict[str, set[str]] = {}
    # 三种类型只需证明存在，不能为找一个类型回表遍历整个账户的广告历史。
    # 保留任意对象存在性，未知历史类型也不能被误当作“目录全空”的活跃账户。
    for advertiser_id, ad_type in session.connection().execute(
        text("""
        SELECT a.advertiser_id, used.ad_type
        FROM unnest(CAST(:accounts AS text[])) a(advertiser_id)
        JOIN LATERAL (
          SELECT true FROM ad_object o
          WHERE o.tenant_id=:tenant AND o.advertiser_id=a.advertiser_id LIMIT 1
        ) present ON true
        LEFT JOIN LATERAL (
          SELECT t.ad_type FROM unnest(CAST(:types AS text[])) t(ad_type)
          JOIN LATERAL (
            SELECT true FROM ad_object o
            WHERE o.tenant_id=:tenant AND o.advertiser_id=a.advertiser_id
              AND o.ad_type=t.ad_type LIMIT 1
          ) found ON true
        ) used ON true
        """),
        {"tenant": tenant_id, "accounts": accounts, "types": list(DIRECTORY_AD_TYPES)},
    ):
        used.setdefault(advertiser_id, set()).add(ad_type or "")
    active = set(active_ids)
    probes = {
        _probe_partition(advertiser_id, ad_type): (advertiser_id, ad_type)
        for advertiser_id in accounts
        for ad_type in DIRECTORY_AD_TYPES
        if ad_type not in used.get(advertiser_id, set())
        and not (advertiser_id in active and not used.get(advertiser_id))
    }
    probed: set[tuple[str, str]] = set()
    if probes:
        # 账户与分区成对探测，从最新请求向后找任一近期成功即可；不能先扫描
        # 整段账户历史再汇总max。保留精确分区合同，不用其他类型/定向任务替代。
        for partition_key in (
            session.connection()
            .execute(
                text("""
            SELECT p.partition_key FROM unnest(
              CAST(:accounts AS text[]),CAST(:partitions AS text[])
            ) p(advertiser_id,partition_key)
            JOIN LATERAL (
              SELECT true FROM ad_directory_run r
              WHERE r.tenant_id=:tenant AND r.advertiser_id=p.advertiser_id
                AND r.partition_key=p.partition_key AND r.status='COMPLETE'
                AND r.completed_at>:cutoff
              ORDER BY r.request_sequence DESC LIMIT 1
            ) fresh ON true
            """),
                {
                    "tenant": tenant_id,
                    "accounts": [advertiser for advertiser, _ in probes.values()],
                    "partitions": list(probes),
                    "cutoff": now - AD_TYPE_PROBE_INTERVAL,
                },
            )
            .scalars()
        ):
            probed.add(probes[partition_key])
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for advertiser_id in accounts:
        in_use = used.get(advertiser_id, set())
        if advertiser_id in active and not in_use:
            result[advertiser_id] = dict(_PERIODIC_KINDS_BY_TYPE)
            continue
        result[advertiser_id] = {
            ad_type: _PERIODIC_KINDS_BY_TYPE[ad_type]
            if ad_type in in_use
            else PROBE_KINDS
            for ad_type in DIRECTORY_AD_TYPES
            if ad_type in in_use or (advertiser_id, ad_type) not in probed
        }
    return result


def directory_due_accounts(
    session: Session,
    *,
    tenant_id: UUID,
    bc_id: str,
    actor_id: UUID,
    frozen_route: dict,
    advertiser_ids: Collection[str],
    active_ids: Collection[str],
    now: datetime,
) -> list[str]:
    """本轮周期目录需要刷新的账户。

    最近有消耗的账户每轮都刷新；其余账户距最近一次成功的周期全量超过
    INACTIVE_DIRECTORY_INTERVAL 才刷新，从未成功过的账户立即刷新。
    """
    accounts = sorted(set(advertiser_ids))
    if not accounts:
        return []
    session.flush()
    # 旧全量仍在读取/等待时先让它完成；在扫描 ad_object 大表前去掉重叠账户。
    # 定向回读和不同冻结授权不能替代本轮全量，不纳入这个周期去重范围。
    # 先物化当前范围内的待执行集合，避免账户IN使优化器交叉扫描大量历史索引。
    pending = set(
        session.connection()
        .execute(
            text("""
            WITH pending AS MATERIALIZED (
              SELECT advertiser_id FROM ad_directory_run
              WHERE tenant_id=:tenant AND bc_id=:bc AND actor_id=:actor
                AND frozen_route=CAST(:route AS jsonb)
                AND status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
                AND (query->>'include_deleted')::boolean IS TRUE
                AND coalesce(jsonb_array_length(query->'ids'),0)=0
                AND coalesce(jsonb_array_length(query->'parent_ids'),0)=0
            )
            SELECT DISTINCT advertiser_id FROM pending WHERE advertiser_id=ANY(:ids)
            """),
            {
                "tenant": tenant_id,
                "bc": bc_id,
                "actor": actor_id,
                "route": json.dumps(frozen_route),
                "ids": accounts,
            },
        )
        .scalars()
    )
    accounts = [
        advertiser_id for advertiser_id in accounts if advertiser_id not in pending
    ]
    active = set(active_ids) & set(accounts)
    idle = [advertiser_id for advertiser_id in accounts if advertiser_id not in active]
    fresh: set[str] = set()
    if idle:
        # 只需任一最近成功证据；逐账户找到首条即停，避免 DISTINCT 扫完运行历史。
        # 保留原有成功范围：不额外收窄 BC、冻结路由或定向 ID 条件。
        fresh = set(
            session.connection()
            .execute(
                text("""
                SELECT a.advertiser_id FROM unnest(CAST(:accounts AS text[])) a(advertiser_id)
                JOIN LATERAL (
                  SELECT 1 FROM ad_directory_run r
                  WHERE r.tenant_id=:tenant AND r.advertiser_id=a.advertiser_id
                    AND r.status='COMPLETE'
                    AND CAST(r.query->>'include_deleted' AS boolean) IS true
                    AND r.completed_at > :cutoff LIMIT 1
                ) recent ON true
                """),
                {
                    "tenant": tenant_id,
                    "accounts": idle,
                    "cutoff": now - INACTIVE_DIRECTORY_INTERVAL,
                },
            )
            .scalars()
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
    include_deleted: bool = False,
    workload: str | None = None,
    dispatch_immediately: bool = True,
) -> int:
    """ad_types 为空表示请求全部类型全部层级；周期调度传入 directory_ad_types 的裁剪结果。

    同一账户同一查询已有尚未开始的运行时不再新建：积压期间每轮的周期请求
    不会层层叠加，排队中的那一条开始执行时自然读到最新数据。
    历史身份补录可显式包含已删除对象；仍按指定ID有界读取，不扩大为全账户扫描。
    周期定向刷新关闭立即入队，只唤醒公平派发；操作后的显式回读保留立即投递。
    """
    if not settings.ADS_SYNC_ENABLED or frozen_route.get("channel") != "OFFICIAL_API":
        return 0
    workload = workload or ("live" if scope == "targeted" else "background")
    if workload not in ("live", "background"):
        raise DomainError("directory_request_invalid", "目录服务分类无效")
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
        str(actor_id),
        advertiser_ids,
        scope,
        sorted((str(r.tenant_id), r.advertiser_id, r.kind, r.remote_id) for r in refs),
        int(now.timestamp()) // 1800,
    ]
    slot_id = uuid5(
        UUID("c5c96c13-7e3a-4cbb-a7cc-9b12e9d1c6ad"),
        json.dumps(payload, sort_keys=True),
    )
    # 每次操作后的回读都必须晚于该次写入；旧排队任务的请求序号也可能已经过时。
    request_id = uuid4() if scope == "targeted" else slot_id
    # 同一槽并发请求串行检查，避免重复插入目录任务。
    session.connection().execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": slot_id.int % (2**63)}
    )
    created = 0
    refill_run = None
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
            # 明确对象 ID 时不再附带父级筛选：分层刷新的父子候选未必相同，
            # 两种条件取交集会漏掉本次明确要求读取的对象。创意接口也不接受父级。
            if ids or kind == "creative":
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
                    "include_deleted": scope == "directory" or include_deleted,
                }
                partition = _partition_key(query)
                existing = session.exec(
                    select(AdDirectoryRun.id)
                    .where(
                        AdDirectoryRun.tenant_id == tenant_id,
                        AdDirectoryRun.advertiser_id == advertiser_id,
                        AdDirectoryRun.partition_key == partition,
                        AdDirectoryRun.bc_id == bc_id,
                        AdDirectoryRun.actor_id == actor_id,
                        AdDirectoryRun.frozen_route == frozen_route,
                        # 已被领取的运行可能已读过首页，不能代替本次刷新。
                        (col(AdDirectoryRun.request_id) == request_id)
                        | and_(
                            scope == "directory",
                            col(AdDirectoryRun.status) == "QUEUED",
                            col(AdDirectoryRun.claim_token).is_(None),
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
                    workload=workload,
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
                if scope == "targeted":
                    session.flush()
                    if dispatch_immediately:
                        enqueue_directory_step(session, run, initial=True)
                    elif refill_run is None:
                        refill_run = run
                created += 1
    session.flush()
    if refill_run is not None:
        # 整批只发一次同事务唤醒，空槽无需等待 Beat；统一派发器先服务已有积压。
        # 使用请求专属键，不能吞掉同一运行完成后的补位信号。
        enqueue_after_commit(
            session,
            context=context,
            task_name="reporting.plan_due",
            task_key=f"directory-request-refill:{refill_run.id}:{refill_run.claim_generation}",
            payload={"dispatch_only": True},
        )
    return created
