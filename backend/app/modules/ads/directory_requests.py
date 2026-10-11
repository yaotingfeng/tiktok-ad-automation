"""广告目录请求独立入队；冻结路由及每半小时槽幂等，发送由目录 Worker 完成。"""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.routing import verify_route
from app.modules.ads.sync_models import AdDirectoryRun


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
) -> int:
    if not settings.ADS_SYNC_ENABLED or frozen_route.get("channel") != "OFFICIAL_API":
        return 0
    route = FrozenTikTokRoute.model_validate(frozen_route)
    advertiser_ids = tuple(sorted(set(advertiser_ids)))
    if (
        route.tenant_id != tenant_id
        or route.bc_id != bc_id
        or scope not in ("directory", "active", "targeted")
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
        directory_specs = (
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
        for kind, ad_type, page_size in directory_specs:
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
                partition = hashlib.sha256(
                    json.dumps(query, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                existing = session.exec(
                    select(AdDirectoryRun)
                    .where(
                        AdDirectoryRun.tenant_id == tenant_id,
                        AdDirectoryRun.request_id == request_id,
                        AdDirectoryRun.advertiser_id == advertiser_id,
                        AdDirectoryRun.partition_key == partition,
                    )
                    .order_by(col(AdDirectoryRun.created_at))
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
