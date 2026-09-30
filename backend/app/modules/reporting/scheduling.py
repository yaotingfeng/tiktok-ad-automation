"""持久化广告/报表同步计划。

计划只保存冻结的连接路由和内部对象引用，真正的 TikTok 请求由后续任务在
有界 worker 中执行。这里不根据当前时间制造“半小时消耗”，日期窗口只决定
需要请求的事实范围；实际覆盖以 ``ReportCoverage`` 发布记录为准。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.accounts.routing import verify_route
from app.modules.ads.models import AdObject
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.contracts import CORE_METRICS, report_partition_key
from app.modules.reporting.models import ReportFact
from app.modules.reporting.sync_models import ReportSyncRun, SyncSchedule

SyncScope = Literal[
    "directory", "active", "report", "balance", "history", "targeted"
]
_SCOPES = frozenset({"directory", "active", "report", "balance", "history", "targeted"})
_SCHEDULE_SECONDS = {
    "directory": 3 * 60 * 60,
    "active": 30 * 60,
    "report": 30 * 60,
    "balance": 30 * 60,
    "history": 7 * 24 * 60 * 60,
}
_SCHEDULE_WINDOWS = {
    "directory": ("rolling",),
    "active": ("rolling",),
    "balance": ("rolling",),
    "report": ("core",),
    "history": ("initial", "attribution", "weekly90d"),
}
_REQUEST_NAMESPACE = UUID("c5c96c13-7e3a-4cbb-a7cc-9b12e9d1c6ad")


@dataclass(frozen=True, slots=True)
class SyncRequest:
    """用户刷新或管理后定向刷新的本地请求合同。"""

    route: FrozenTikTokRoute
    advertiser_ids: tuple[str, ...]
    scope: SyncScope
    start_date: date | None = None
    end_date: date | None = None
    refs: tuple[EntityRef, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.route, FrozenTikTokRoute):
            raise TypeError("route must be a FrozenTikTokRoute")
        if self.scope not in _SCOPES:
            raise ValueError("unsupported sync scope")
        ids = tuple(dict.fromkeys(self.advertiser_ids))
        if not ids or any(type(value) is not str or not value.strip() for value in ids):
            raise ValueError("advertiser_ids must contain non-empty strings")
        if ids != self.advertiser_ids:
            object.__setattr__(self, "advertiser_ids", ids)
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date and end_date must be supplied together")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("sync date range is inverted")
        refs = tuple(self.refs)
        if any(
            not isinstance(ref, EntityRef)
            or ref.tenant_id != self.route.tenant_id
            or ref.advertiser_id not in ids
            for ref in refs
        ):
            raise ValueError("sync refs are outside the request scope")
        if refs != self.refs:
            object.__setattr__(self, "refs", refs)


def planned_window(
    *,
    kind: str,
    now: datetime | None = None,
    timezone: str = "UTC",
    attribution_days: int | None = None,
) -> timedelta:
    """Return a durable date span; no synthetic interval metrics are produced.

    ``unknown_attribution`` deliberately uses 35 days.  Once a platform reports a
    larger attribution window, callers pass it through ``attribution_days`` and the
    seven-day safety buffer expands the next request accordingly.
    """

    del now, timezone  # kept in the signature so callers can use one clock contract
    if kind == "initial":
        days = 30
    elif kind in {"unknown_attribution", "attribution_unknown"}:
        days = 35
    elif kind in {"attribution", "attribution_backfill"}:
        if attribution_days is None:
            days = 35
        elif type(attribution_days) is not int or attribution_days < 0:
            raise ValueError("attribution_days must be a non-negative integer")
        else:
            days = attribution_days + 7
    elif kind in {"core", "today_prior_day"}:
        days = 2
    elif kind in {"recent7d", "recent_7d"}:
        days = 7
    elif kind in {"weekly90d", "weekly_90d"}:
        days = 90
    elif kind in {"directory", "active", "balance"}:
        days = 1
    else:
        raise ValueError(f"unknown sync window: {kind}")
    return timedelta(days=days)


def _local_today(account: AdvertiserAccount, now: datetime) -> date:
    try:
        return now.astimezone(ZoneInfo(account.timezone)).date()
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise DomainError("account_metadata_incomplete", "广告账户时区无效") from exc


def _dates(
    kind: str,
    *,
    account: AdvertiserAccount,
    now: datetime,
    attribution_days: int | None = None,
) -> tuple[date | None, date | None]:
    today = _local_today(account, now)
    if kind in {"directory", "active", "balance"}:
        return None, None
    span = planned_window(
        kind=kind,
        now=now,
        timezone=account.timezone,
        attribution_days=attribution_days,
    ).days
    # Report buckets are complete calendar days.  Core explicitly covers local
    # today and yesterday; the platform may return a partial current-day bucket.
    end = today
    return end - timedelta(days=span - 1), end


def _next_due(window: str, now: datetime) -> datetime:
    """Return the next durable tick; attribution is anchored at 13:00 UTC."""

    if window != "attribution":
        return now
    due = now.replace(hour=13, minute=0, second=0, microsecond=0)
    return due if due > now else due + timedelta(days=1)


def _route_dump(route: FrozenTikTokRoute) -> dict[str, Any]:
    return route.model_dump(mode="json")


def _route_digest(route: FrozenTikTokRoute) -> str:
    encoded = json.dumps(_route_dump(route), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]


def schedule_key(
    *, route: FrozenTikTokRoute, advertiser_id: str, scope: str, window: str
) -> str:
    """Stable short key including authorization and binding generations."""

    # The digest contains channel, connection, authorization_revision and
    # binding_revision.  It is part of the unique key so a rebind can never merge
    # work with the old authorization semantics.
    return f"{scope}:{window}:{advertiser_id[:12]}:{_route_digest(route)}"[:64]


def _request_id(request: SyncRequest) -> UUID:
    key = {
        "route": _route_dump(request.route),
        "advertiser_ids": sorted(request.advertiser_ids),
        "scope": request.scope,
        "start_date": request.start_date.isoformat() if request.start_date else None,
        "end_date": request.end_date.isoformat() if request.end_date else None,
        "refs": [
            {
                "tenant_id": str(ref.tenant_id),
                "advertiser_id": ref.advertiser_id,
                "kind": ref.kind,
                "remote_id": ref.remote_id,
            }
            for ref in sorted(request.refs, key=lambda item: (item.advertiser_id, item.kind, item.remote_id))
        ],
    }
    encoded = json.dumps(key, sort_keys=True, separators=(",", ":"))
    return uuid5(_REQUEST_NAMESPACE, encoded)


def _validate_accounts(session: Session, *, context: TenantContext, request: SyncRequest) -> None:
    if context.tenant_id != request.route.tenant_id:
        raise DomainError("connection_tenant_mismatch", "同步请求租户不匹配")
    # Validate the frozen route once without requiring a potentially stale account
    # row, then validate every selected account against its current BC grant.
    verify_route(
        session,
        context=context,
        route=request.route,
        advertiser_id=None,
        capability="read",
    )
    for advertiser_id in request.advertiser_ids:
        verify_route(
            session,
            context=context,
            route=request.route,
            advertiser_id=advertiser_id,
            capability="read",
        )


def _query_for(
    account: AdvertiserAccount,
    *,
    start_date: date,
    end_date: date,
    refs: tuple[EntityRef, ...],
) -> dict[str, Any]:
    filter_ids = sorted(
        {
            ref.remote_id
            for ref in refs
            if ref.advertiser_id == account.advertiser_id
            and ref.kind in {"campaign", "adgroup", "ad"}
        }
    )
    # The account-level contract is intentionally used for the durable scheduler
    # seed.  Object-level runs are added by the collector after directory membership
    # is known; this avoids inventing a campaign total from child rows.
    return {
        "advertiser_id": account.advertiser_id,
        "report_contract": "basic_campaign",
        "metric_family": "delivery",
        "dimensions": ["campaign_id", "stat_time_day"],
        "metrics": list(CORE_METRICS),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "granularity": "DAY",
        "currency": account.currency,
        "timezone": account.timezone,
        "attribution": "default",
        "filter_ids": filter_ids,
        "page": 1,
    }


def _existing_run(
    session: Session,
    *,
    tenant_id: UUID,
    request_id: UUID,
    advertiser_id: str,
    partition: str,
) -> ReportSyncRun | None:
    return session.exec(
        select(ReportSyncRun)
        .where(
            ReportSyncRun.request_id == request_id,
            ReportSyncRun.tenant_id == tenant_id,
            ReportSyncRun.advertiser_id == advertiser_id,
            ReportSyncRun.partition_key == partition,
        )
        .order_by(col(ReportSyncRun.created_at))
    ).first()


def request_sync(session: Session, *, context: TenantContext, request: SyncRequest) -> UUID:
    """Create or return a durable report run for one logical refresh request.

    The request UUID is deterministic over the frozen route, IDs, references and
    date window.  This makes repeated button presses and concurrent deliveries
    converge on the same run while a route rebind produces a new request identity.
    """

    _validate_accounts(session, context=context, request=request)
    request_id = _request_id(request)
    # PostgreSQL advisory lock closes the select-then-insert race for two browser
    # clicks or two Beat deliveries.  It is transaction-scoped and never held over
    # a TikTok request.
    SASession.execute(
        session,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"report-sync:{request.route.tenant_id}:{request_id}"},
    )
    now = datetime.now(UTC)
    if request.scope in {"directory", "active"}:
        return _request_directory_sync(
            session,
            context=context,
            request=request,
            request_id=request_id,
            now=now,
        )
    selected: list[ReportSyncRun] = []
    for advertiser_id in request.advertiser_ids:
        account = session.get(
            AdvertiserAccount, (context.tenant_id, advertiser_id), populate_existing=True
        )
        if account is None:
            raise DomainError("account_not_in_bc", "广告账户不存在")
        if request.start_date is not None:
            start_date, end_date = request.start_date, request.end_date
        elif request.scope == "history":
            start_date, end_date = _dates("initial", account=account, now=now)
        else:
            start_date, end_date = _dates("core", account=account, now=now)
        assert start_date is not None and end_date is not None
        query = _query_for(
            account, start_date=start_date, end_date=end_date, refs=request.refs
        )
        partition = report_partition_key(
            # This compact DTO is built by decode_query to preserve the canonical
            # contract serialization used by A5/A6.
            _decode_query(query)
        )
        existing = _existing_run(
            session,
            request_id=request_id,
            tenant_id=context.tenant_id,
            advertiser_id=advertiser_id,
            partition=partition,
        )
        if existing is not None:
            selected.append(existing)
            continue
        run = ReportSyncRun(
            tenant_id=context.tenant_id,
            advertiser_id=advertiser_id,
            bc_id=request.route.bc_id,
            actor_id=context.actor_id,
            connection_id=request.route.connection_id,
            channel=request.route.channel,
            frozen_route=_route_dump(request.route),
            request_id=request_id,
            partition_key=partition,
            query=query,
            status="QUEUED",
            claim_generation=1,
            next_page=1,
            coverage="PENDING",
            observed_at=now,
            task_id=f"sync:{request.scope}",
        )
        session.add(run)
        selected.append(run)
    session.flush()
    return selected[0].id


def _request_directory_sync(
    session: Session,
    *,
    context: TenantContext,
    request: SyncRequest,
    request_id: UUID,
    now: datetime,
) -> UUID:
    """Create one bounded directory run per hierarchy level for a plan."""

    selected: list[AdDirectoryRun] = []
    for advertiser_id in request.advertiser_ids:
        account_refs = tuple(
            ref for ref in request.refs if ref.advertiser_id == advertiser_id
        )
        for kind, ad_type, page_size in (
            ("campaign", "REGULAR", 1000),
            ("adgroup", "REGULAR", 1000),
            ("ad", "REGULAR", 100),
        ):
            ids = tuple(sorted({ref.remote_id for ref in account_refs if ref.kind == kind}))
            parent_kind = {
                "campaign": (),
                "adgroup": ("campaign",),
                "ad": ("adgroup",),
            }[kind]
            parent_ids = tuple(
                sorted({ref.remote_id for ref in account_refs if ref.kind in parent_kind})
            )
            query = {
                "advertiser_id": advertiser_id,
                "kind": kind,
                "ad_type": ad_type,
                "page": 1,
                "page_size": page_size,
                "ids": list(ids),
                "parent_ids": list(parent_ids),
                "include_deleted": request.scope == "directory",
            }
            partition = hashlib.sha256(
                json.dumps(query, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            existing = session.exec(
                select(AdDirectoryRun)
                .where(
                    AdDirectoryRun.tenant_id == context.tenant_id,
                    AdDirectoryRun.request_id == request_id,
                    AdDirectoryRun.advertiser_id == advertiser_id,
                    AdDirectoryRun.partition_key == partition,
                )
                .order_by(col(AdDirectoryRun.created_at))
            ).first()
            if existing is not None:
                selected.append(existing)
                continue
            run = AdDirectoryRun(
                tenant_id=context.tenant_id,
                advertiser_id=advertiser_id,
                bc_id=request.route.bc_id,
                actor_id=context.actor_id,
                connection_id=request.route.connection_id,
                channel=request.route.channel,
                frozen_route=_route_dump(request.route),
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
            selected.append(run)
    session.flush()
    return selected[0].id


def _decode_query(value: dict[str, Any]):
    from app.modules.reporting.contracts import decode_query

    return decode_query(value)


def ensure_sync_schedules(
    session: Session,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    advertiser_ids: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> tuple[SyncSchedule, ...]:
    """Create the fixed plans for current grants and disable unbound plans."""

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    verify_route(session, context=context, route=route, advertiser_id=None, capability="read")
    requested = set(advertiser_ids or ())
    grants = session.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == route.tenant_id,
            BCAccountAccess.bc_id == route.bc_id,
            BCAccountAccess.connection_id == route.connection_id,
            col(BCAccountAccess.in_bc).is_(True),
            col(BCAccountAccess.authorized).is_(True),
            col(BCAccountAccess.active).is_(True),
        )
    ).all()
    if requested:
        grants = [grant for grant in grants if grant.advertiser_id in requested]
    active_ids = {grant.advertiser_id for grant in grants}
    # Removing a BC grant must stop future scans immediately, including plans from
    # an older binding generation.  Historical run facts remain queryable.
    if not requested:
        for row in session.exec(
            select(SyncSchedule).where(
                SyncSchedule.tenant_id == route.tenant_id,
                SyncSchedule.bc_id == route.bc_id,
                col(SyncSchedule.enabled).is_(True),
            )
        ).all():
            if row.advertiser_id not in active_ids:
                row.enabled = False
                row.error_code = "account_unbound"
    result: list[SyncSchedule] = []
    for grant in grants:
        account = session.get(
            AdvertiserAccount,
            (route.tenant_id, grant.advertiser_id),
            populate_existing=True,
        )
        if account is None:
            continue
        # A rebinding creates a fresh generation of every plan.  Old generations
        # remain as audit rows but are disabled before the new plan can be claimed.
        current_route_json = _route_dump(route)
        for stale in session.exec(
            select(SyncSchedule).where(
                SyncSchedule.tenant_id == route.tenant_id,
                SyncSchedule.bc_id == route.bc_id,
                SyncSchedule.advertiser_id == account.advertiser_id,
                col(SyncSchedule.enabled).is_(True),
            )
        ).all():
            if stale.frozen_route != current_route_json:
                stale.enabled = False
                stale.error_code = "route_binding_changed"
        for scope in _SCHEDULE_WINDOWS:
            windows = (
                ("core", "recent7d") if scope == "report" else _SCHEDULE_WINDOWS[scope]
            )
            for window in windows:
                assert window is not None
                key = schedule_key(
                    route=route,
                    advertiser_id=account.advertiser_id,
                    scope=scope,
                    window=window,
                )
                row = session.exec(
                    select(SyncSchedule).where(
                        SyncSchedule.tenant_id == route.tenant_id,
                        SyncSchedule.bc_id == route.bc_id,
                        SyncSchedule.advertiser_id == account.advertiser_id,
                        SyncSchedule.scope == scope,
                        SyncSchedule.schedule_key == key,
                    )
                ).one_or_none()
                start_date, end_date = (
                    _dates("directory", account=account, now=now)
                    if window == "rolling"
                    else _dates(window, account=account, now=now)
                )
                if row is None:
                    row = SyncSchedule(
                        tenant_id=route.tenant_id,
                        bc_id=route.bc_id,
                        advertiser_id=account.advertiser_id,
                        actor_id=context.actor_id,
                        connection_id=route.connection_id,
                        channel=route.channel,
                        frozen_route=_route_dump(route),
                        schedule_key=key,
                        scope=scope,
                        enabled=True,
                        interval_seconds=(
                            3 * 60 * 60
                            if window == "recent7d"
                            else _SCHEDULE_SECONDS.get(scope, 30 * 60)
                        ),
                        next_due_at=_next_due(window, now),
                        start_date=start_date,
                        end_date=end_date,
                        refs=[],
                    )
                    session.add(row)
                else:
                    row.enabled = True
                    row.actor_id = context.actor_id
                    row.frozen_route = _route_dump(route)
                    row.next_due_at = min(row.next_due_at, _next_due(window, now))
                    row.start_date, row.end_date = start_date, end_date
                    row.error_code = None
                result.append(row)
    session.flush()
    return tuple(result)


# Name used by a few callers during the migration from “seed” terminology.
seed_sync_schedules = ensure_sync_schedules


def enqueue_due_syncs(session: Session, *, now: datetime) -> tuple[UUID, ...]:
    """Claim due plans and create bounded report runs in the caller transaction."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    rows = session.exec(
        select(SyncSchedule)
        .where(
            col(SyncSchedule.enabled).is_(True),
            SyncSchedule.next_due_at <= now,
            or_(
                col(SyncSchedule.claimed_until).is_(None),
                col(SyncSchedule.claimed_until) <= now,
            ),
        )
        .order_by(col(SyncSchedule.next_due_at), col(SyncSchedule.id))
        .with_for_update(skip_locked=True)
    ).all()
    result: list[UUID] = []
    for schedule in rows:
        route = FrozenTikTokRoute.model_validate(schedule.frozen_route)
        # Re-read the schedule's background actor and current membership before
        # creating any run. A revoked actor or unbound account disables future work.
        context = TenantContext(
            tenant_id=schedule.tenant_id,
            actor_id=schedule.actor_id,
            role="operator",
        )
        try:
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=schedule.advertiser_id,
                capability="read",
            )
        except DomainError as exc:
            schedule.enabled = False
            schedule.error_code = exc.code
            schedule.claimed_until = schedule.claim_token = None
            continue
        refs = tuple(
            ref
            for item in schedule.refs
            if (ref := _ref_from_json(item)) is not None
        )
        if schedule.scope in {"active", "report"}:
            # Membership is read from current directory plus recent spend facts;
            # stopped objects inside the attribution horizon remain eligible.
            refs = active_or_recent_refs(
                session,
                tenant_id=schedule.tenant_id,
                advertiser_id=schedule.advertiser_id,
                now=now,
                attribution_days=35 if schedule.scope == "report" else None,
            )
        request = SyncRequest(
            route=route,
            advertiser_ids=(schedule.advertiser_id,),
            scope=cast(SyncScope, schedule.scope),
            start_date=schedule.start_date,
            end_date=schedule.end_date,
            refs=refs,
        )
        run_id = request_sync(session, context=context, request=request)
        result.append(run_id)
        schedule.last_request_id = run_id
        schedule.requested_coverage = {
            "start_date": schedule.start_date.isoformat() if schedule.start_date else None,
            "end_date": schedule.end_date.isoformat() if schedule.end_date else None,
            "requested_at": now.isoformat(),
        }
        # Advance from the prior due timestamp, not from wall-clock now, and skip
        # missed ticks. This keeps one long run from recreating a backlog each cycle.
        due = schedule.next_due_at
        step = timedelta(seconds=schedule.interval_seconds)
        while due <= now:
            due += step
        schedule.next_due_at = due
        schedule.claim_generation += 1
        schedule.claim_token = None
        schedule.claimed_until = None
    session.flush()
    return tuple(result)


def record_schedule_completion(
    session: Session,
    *,
    schedule_id: UUID,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Advance completed coverage only from published, complete partitions.

    A queued request is deliberately not counted as coverage.  This helper is
    called by a collector after ``publish_report`` (and is safe to repeat).
    """

    schedule = session.get(SyncSchedule, schedule_id, populate_existing=True)
    if schedule is None:
        raise DomainError("sync_schedule_not_found", "同步计划不存在")
    if start_date > end_date:
        raise ValueError("coverage range is inverted")
    current = schedule.completed_coverage if isinstance(schedule.completed_coverage, dict) else {}
    ranges = list(current.get("ranges", [])) if isinstance(current.get("ranges", []), list) else []
    item = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
    if item not in ranges:
        ranges.append(item)
    # A schedule's completed range is an evidence summary, not a synthetic union:
    # retain every non-contiguous published range for the query layer to inspect.
    schedule.completed_coverage = {
        "ranges": sorted(ranges, key=lambda value: (value["start_date"], value["end_date"])),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    session.add(schedule)
    session.flush()
    return schedule.completed_coverage


def _ref_from_json(value: Any) -> EntityRef | None:
    if not isinstance(value, dict):
        return None
    try:
        return EntityRef(
            UUID(str(value["tenant_id"])),
            str(value["advertiser_id"]),
            value["kind"],
            str(value["remote_id"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def active_or_recent_refs(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    now: datetime,
    recent_days: int = 7,
    attribution_days: int | None = None,
) -> tuple[EntityRef, ...]:
    """Return enabled or recently-spending objects, including stopped attribution."""

    if now.tzinfo is None or type(recent_days) is not int or recent_days < 0:
        raise ValueError("invalid membership window")
    horizon = max(recent_days, attribution_days or 0)
    cutoff = now - timedelta(days=horizon)
    refs: set[EntityRef] = set()
    for obj in session.exec(
        select(AdObject).where(
            AdObject.tenant_id == tenant_id,
            AdObject.advertiser_id == advertiser_id,
            col(AdObject.operation_status).in_(["ENABLE", "STATUS_ENABLE"]),
        )
    ).all():
        refs.add(obj.ref)
    facts = session.exec(
        select(ReportFact).where(
            ReportFact.tenant_id == tenant_id,
            ReportFact.advertiser_id == advertiser_id,
            ReportFact.metric_name == "spend",
            ReportFact.bucket_end > cutoff,
            ReportFact.bucket_start <= now,
        )
    ).all()
    for fact in facts:
        if fact.value is not None and fact.value > 0 and len(fact.subject_key) >= 2:
            kind = str(fact.subject_key[0])
            if kind in {"campaign", "adgroup", "ad", "creative"}:
                refs.add(EntityRef(tenant_id, advertiser_id, kind, str(fact.subject_key[1])))
    return tuple(sorted(refs, key=lambda ref: (ref.kind, ref.remote_id)))


active_or_recent_spend_membership = active_or_recent_refs


__all__ = [
    "SyncRequest",
    "planned_window",
    "request_sync",
    "ensure_sync_schedules",
    "seed_sync_schedules",
    "enqueue_due_syncs",
    "active_or_recent_refs",
    "active_or_recent_spend_membership",
    "record_schedule_completion",
    "schedule_key",
]
