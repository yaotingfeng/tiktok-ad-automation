"""基于已发布事实的趋势读取。

``ReportObservation`` 目前没有被发布路径稳定追加，因此趋势不能从 ReportFact
硬拼历史点。只有在同一授权范围内存在对应生产事实时才消费观测；无事实/无观测
分别返回 UNSUPPORTED/INCOMPLETE 覆盖状态。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal, cast

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.ads.models import CampaignNameProjection
from app.modules.reporting.filters import authorized_grants, compile_filter
from app.modules.reporting.models import ReportFact, ReportObservation
from app.modules.reporting.schemas import (
    Availability,
    ReportingFilter,
    TrendPoint,
    TrendPublic,
)
from app.modules.tenants.permissions import require_tenant


def _period(filters: ReportingFilter) -> tuple[datetime, datetime]:
    start = datetime.combine(filters.start_date, time.min, tzinfo=UTC)
    end = datetime.combine(filters.end_date + timedelta(days=1), time.min, tzinfo=UTC)
    return start, end


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _observation_kind(dimension: str) -> str | None:
    if dimension == "account":
        return "account"
    if dimension in {"campaign", "drama"}:
        return "campaign"
    return None


def _matches_filter(observation: ReportObservation, filters: ReportingFilter) -> bool:
    if filters.ids:
        subject_id = observation.subject_key[-1] if observation.subject_key else ""
        if subject_id not in filters.ids and ":".join(observation.subject_key) not in filters.ids:
            return False
    return True


def _bucket_start(value: datetime, grain: Literal["hour", "day", "observation"]) -> datetime:
    if grain == "observation":
        return value
    value = value.astimezone(UTC)
    if grain == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _has_production_facts(
    session: Session,
    *,
    tenant_id: object,
    advertiser_ids: Sequence[str],
    start: datetime,
    end: datetime,
) -> bool:
    if not advertiser_ids:
        return False
    statement = select(ReportFact.id).where(
        ReportFact.tenant_id == tenant_id,
        col(ReportFact.advertiser_id).in_(advertiser_ids),
        ReportFact.bucket_start < end,
        ReportFact.bucket_end > start,
    ).limit(1)
    return session.exec(statement).first() is not None


def _fact_for_observation(
    session: Session, observation: ReportObservation
) -> bool:
    # The explicit tenant predicate is intentional: subject IDs alone are not
    # tenant scoped, and observations are historical rather than authorization.
    statement = select(ReportFact.id).where(
        ReportFact.tenant_id == observation.tenant_id,
        ReportFact.advertiser_id == observation.advertiser_id,
        ReportFact.subject_key == observation.subject_key,
        ReportFact.bucket_start == observation.bucket_start,
        ReportFact.bucket_end == observation.bucket_end,
    ).limit(1)
    return session.exec(statement).first() is not None


@dataclass(frozen=True)
class _Series:
    """A comparable point after optional drama re-projection."""

    advertiser_id: str
    subject_key: tuple[str, ...]
    bucket_start: datetime
    bucket_end: datetime
    values: dict[str, str | None]
    availability: dict[str, str]
    observed_at: datetime
    membership_digest: str
    grouping_revision: int
    currency: str
    timezone: str
    attribution: str
    report_contract: str
    metric_family: str


def _series_from_observation(item: ReportObservation) -> _Series:
    return _Series(
        advertiser_id=item.advertiser_id,
        subject_key=tuple(item.subject_key),
        bucket_start=item.bucket_start,
        bucket_end=item.bucket_end,
        values=item.values,
        availability=item.availability,
        observed_at=item.observed_at,
        membership_digest=item.membership_digest,
        grouping_revision=item.grouping_revision,
        currency=item.currency,
        timezone=item.timezone,
        attribution=item.attribution,
        report_contract=item.report_contract,
        metric_family=item.metric_family,
    )


def _drama_series(
    observations: Sequence[ReportObservation],
    projections: Sequence[CampaignNameProjection],
    *,
    bc_id: str,
) -> tuple[_Series, ...]:
    latest: dict[tuple[str, str], CampaignNameProjection] = {}
    for projection in projections:
        key = (projection.advertiser_id, projection.campaign_remote_id)
        prior = latest.get(key)
        if prior is None or (
            projection.grouping_revision,
            projection.name_revision,
            projection.observed_at,
        ) > (prior.grouping_revision, prior.name_revision, prior.observed_at):
            latest[key] = projection

    # A publish version is the membership snapshot.  Grouping by it retains a
    # newer same-bucket correction as a separate point instead of dropping it.
    grouped: dict[tuple[str, datetime, datetime, int], list[ReportObservation]] = defaultdict(list)
    group_keys: dict[tuple[str, datetime, datetime, int], tuple[str, ...]] = {}
    for item in observations:
        campaign_id = item.subject_key[-1] if item.subject_key else ""
        latest_projection = latest.get((item.advertiser_id, campaign_id))
        if latest_projection is not None and latest_projection.status == "VALID":
            group_id = f"{bc_id}:drama:{latest_projection.provider_label}:{latest_projection.drama_name}"
            subject_key = (group_id,)
        else:
            group_id = f"{bc_id}:external:{item.advertiser_id}:{campaign_id}"
            subject_key = (group_id,)
        bucket_key = (group_id, item.bucket_start, item.bucket_end, item.published_version)
        grouped[bucket_key].append(item)
        group_keys[bucket_key] = subject_key

    availability_rank = {"AVAILABLE": 0, "MISSING": 1, "UNAVAILABLE": 2, "UNSUPPORTED": 3, "FAILED": 4}
    result: list[_Series] = []
    for bucket_key, members in grouped.items():
        _group_id, bucket_start, bucket_end, _version = bucket_key
        metric_names = sorted({name for member in members for name in member.values})
        values: dict[str, str | None] = {}
        availability: dict[str, str] = {}
        for name in metric_names:
            states = [member.availability.get(name, "MISSING") for member in members]
            state = max(states, key=lambda value: availability_rank.get(value, 4))
            availability[name] = state
            if state != "AVAILABLE" or any(member.availability.get(name) != "AVAILABLE" for member in members):
                values[name] = None
                continue
            numbers = [_decimal(member.values.get(name)) for member in members]
            values[name] = str(sum((number for number in numbers if number is not None), Decimal(0)))
        digest_input = "|".join(sorted(f"{member.advertiser_id}:{member.membership_digest}" for member in members))
        first = members[0]
        result.append(_Series(
            advertiser_id="__drama__",
            subject_key=group_keys[bucket_key],
            bucket_start=bucket_start,
            bucket_end=bucket_end,
            values=values,
            availability=availability,
            observed_at=max(member.observed_at for member in members),
            membership_digest=sha256(digest_input.encode()).hexdigest(),
            grouping_revision=max(member.grouping_revision for member in members),
            currency=first.currency,
            timezone=first.timezone,
            attribution=first.attribution,
            report_contract=first.report_contract,
            metric_family=first.metric_family,
        ))
    return tuple(result)


def _series_delta(previous: _Series, current: _Series) -> dict[str, Decimal] | None:
    if (
        previous.membership_digest != current.membership_digest
        or previous.grouping_revision != current.grouping_revision
        or previous.currency != current.currency
        or previous.timezone != current.timezone
        or previous.attribution != current.attribution
        or previous.report_contract != current.report_contract
        or previous.metric_family != current.metric_family
    ):
        return None
    result: dict[str, Decimal] = {}
    for name in set(previous.values) & set(current.values):
        if previous.availability.get(name) != "AVAILABLE" or current.availability.get(name) != "AVAILABLE":
            continue
        old, new = _decimal(previous.values[name]), _decimal(current.values[name])
        if old is not None and new is not None:
            result[name] = new - old
    return result


def build_trend(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    filters: ReportingFilter,
    grain: Literal["hour", "day", "observation"],
) -> TrendPublic:
    """读取观测趋势并标注作用域/日期变化原因。"""

    if grain not in {"hour", "day", "observation"}:
        raise ValueError("unsupported trend grain")
    compile_filter(filters)
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read")
    grants = authorized_grants(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_ids=filters.advertiser_ids,
    )
    advertiser_ids = tuple(sorted({grant.advertiser_id for grant in grants}))
    start, end = _period(filters)
    kind = _observation_kind(filters.dimension)
    if kind is None:
        return TrendPublic(
            coverage={"status": "UNSUPPORTED", "reason": "dimension_has_no_observation"},
            delta_reason=None,
        )
    if not _has_production_facts(
        session,
        tenant_id=context.tenant_id,
        advertiser_ids=advertiser_ids,
        start=start,
        end=end,
    ):
        return TrendPublic(
            coverage={"status": "UNSUPPORTED", "reason": "production_facts_unavailable"},
            delta_reason=None,
        )
    observations = tuple(
        session.exec(
            select(ReportObservation).where(
                ReportObservation.tenant_id == context.tenant_id,
                col(ReportObservation.advertiser_id).in_(advertiser_ids),
                ReportObservation.subject_kind == kind,
                ReportObservation.bucket_start < end,
                ReportObservation.bucket_end > start,
            ).order_by(
                col(ReportObservation.bucket_start),
                col(ReportObservation.advertiser_id),
                col(ReportObservation.observed_at),
            )
        ).all()
    )
    observations = tuple(
        item for item in observations
        if _matches_filter(item, filters) and _fact_for_observation(session, item)
    )
    if not observations:
        return TrendPublic(
            coverage={"status": "INCOMPLETE", "reason": "observations_unavailable"},
            delta_reason=None,
        )

    if filters.dimension == "drama":
        projections = tuple(
            session.exec(
                select(CampaignNameProjection).where(
                    CampaignNameProjection.tenant_id == context.tenant_id,
                    col(CampaignNameProjection.advertiser_id).in_(advertiser_ids),
                )
            ).all()
        )
        series = _drama_series(observations, projections, bc_id=bc_id)
    else:
        series = tuple(_series_from_observation(item) for item in observations)

    # Retain same-bucket observations so platform corrections remain visible.
    ordered = sorted(series, key=lambda item: (item.advertiser_id, item.subject_key, item.bucket_start, item.observed_at))
    points: list[TrendPoint] = []
    previous_by_subject: dict[tuple[str, tuple[str, ...]], _Series] = {}
    overall_reason: Literal["SCOPE_CHANGED", "DATE_CHANGED"] | None = None
    intervals: list[int] = []
    for item in ordered:
        reason: Literal["SCOPE_CHANGED", "DATE_CHANGED"] | None = None
        delta: dict[str, Decimal] | None = None
        subject_key = (item.advertiser_id, item.subject_key)
        previous = previous_by_subject.get(subject_key)
        if previous is not None:
            if (
                previous.membership_digest != item.membership_digest
                or previous.grouping_revision != item.grouping_revision
                or previous.subject_key != item.subject_key
            ):
                reason = "SCOPE_CHANGED"
            elif (
                previous.bucket_start != item.bucket_start
                and previous.bucket_end != item.bucket_start
            ):
                reason = "DATE_CHANGED"
            elif previous.bucket_start.date() != item.bucket_start.date():
                reason = "DATE_CHANGED"
            else:
                delta = _series_delta(previous, item)
            intervals.append(int((item.bucket_start - previous.bucket_start).total_seconds() // 60))
            if reason is not None and overall_reason is None:
                overall_reason = reason
        values = {name: _decimal(value) for name, value in item.values.items()}
        availability: dict[str, Availability] = {}
        for name, state in item.availability.items():
            availability[name] = cast(
                Availability,
                state if state in {"AVAILABLE", "MISSING", "UNAVAILABLE", "UNSUPPORTED", "FAILED"} else "FAILED",
            )
        points.append(
            TrendPoint(
                bucket_start=_bucket_start(item.bucket_start, grain),
                bucket_end=item.bucket_end,
                values=values,
                availability=availability,
                delta=delta,
                delta_reason=reason,
            )
        )
        previous_by_subject[subject_key] = item
    interval = min(intervals) if intervals else None
    return TrendPublic(
        points=tuple(points),
        coverage={"status": "COMPLETE", "observations": len(points)},
        actual_interval_minutes=interval,
        delta_reason=overall_reason,
    )
