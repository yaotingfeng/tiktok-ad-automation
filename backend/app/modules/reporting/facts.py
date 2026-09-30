"""报表事实的分片暂存、完整发布及观测差值。

网络采集器只写 ``ReportStagedPage``。本模块在一个数据库事务中确认完整页链、
冻结路由和运行代数，然后以完整指标组替换同一分片的旧事实；不完整响应不能
污染已发布事实，也不能用缺失指标伪造零值。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.reporting import ReportPage, ReportRow
from app.modules.accounts.models import AdvertiserAccount
from app.modules.accounts.routing import verify_route
from app.modules.reporting.contracts import (
    decode_query,
    report_buckets,
    report_partition_key,
    supports_metric,
    validate_query,
)
from app.modules.reporting.models import ReportCoverage, ReportFact, ReportObservation
from app.modules.reporting.sync_models import ReportStagedPage, ReportSyncRun

_ATTRIBUTE_KEYS = frozenset(
    {
        "advertiser_id",
        "campaign_id",
        "campaign_name",
        "adgroup_id",
        "adgroup_name",
        "ad_id",
        "ad_name",
        "ad_id_v2",
        "smart_plus_ad_id",
        "main_material_id",
        "main_material_type",
        "ad_material_id",
        "smart_plus_creative_id",
        "smart_plus_creative_name",
    }
)


def _domain(code: str, message: str) -> DomainError:
    return DomainError(code, message)


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _row_payload(row: ReportRow) -> dict[str, Any]:
    """ReportRow 的 JSON 形式保留 Decimal 字符串，避免 float 降精度。"""
    return {
        "subject_key": list(row.subject_key),
        "bucket_start": row.bucket_start.isoformat(),
        "bucket_end": row.bucket_end.isoformat(),
        "values": {
            key: (None if value is None else str(value))
            for key, value in row.values.items()
        },
        "availability": dict(row.availability),
        "attributes": _json_value(row.attributes),
    }


def _evidence_json(page: ReportPage) -> dict[str, Any]:
    return _json_value(asdict(page.evidence))


def _same_page(
    row: ReportStagedPage,
    page: ReportPage,
    claim_generation: int,
    *,
    require_claim: bool = True,
) -> bool:
    return (
        (not require_claim or row.claim_generation == claim_generation)
        and row.next_page == page.next_page
        and row.complete == page.complete
        and row.rows == [_row_payload(item) for item in page.rows]
        and row.evidence == _evidence_json(page)
    )


def _route_for_run(run: ReportSyncRun) -> FrozenTikTokRoute:
    try:
        route = FrozenTikTokRoute.model_validate(run.frozen_route)
    except (TypeError, ValueError) as exc:
        raise _domain("frozen_route_changed", "报表运行的冻结路由无效") from exc
    if (
        route.tenant_id != run.tenant_id
        or route.bc_id != run.bc_id
        or route.connection_id != run.connection_id
        or route.channel != run.channel
    ):
        raise _domain("frozen_route_scope_mismatch", "报表运行与冻结路由范围不一致")
    return route


def _ensure_route_authority(
    session: Session, run: ReportSyncRun, route: FrozenTikTokRoute
) -> None:
    verify_route(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
        ),
        route=route,
        advertiser_id=run.advertiser_id,
        capability="read",
    )


def _complete_chain(pages: list[ReportStagedPage]) -> list[ReportStagedPage]:
    by_number = {row.page: row for row in pages}
    if not by_number or 1 not in by_number:
        raise _domain("report_incomplete", "报表暂存缺少第一页")
    result: list[ReportStagedPage] = []
    page_no = 1
    seen: set[int] = set()
    while True:
        row = by_number.get(page_no)
        if row is None or row.page in seen:
            raise _domain("report_incomplete", "报表分页存在缺页")
        seen.add(row.page)
        result.append(row)
        if row.complete:
            if row.next_page is not None:
                raise _domain("report_incomplete", "完整报表页不能继续分页")
            break
        if row.next_page is None:
            raise _domain("report_incomplete", "报表中间页没有后续页")
        page_no = row.next_page
    if len(seen) != len(by_number):
        raise _domain("report_incomplete", "报表暂存包含断开的页")
    return result


def stage_report_page(
    session: Session,
    *,
    run_id: UUID,
    page: ReportPage,
    claim_generation: int,
) -> None:
    """写入不可变报表页；相同重试幂等，冲突证据永不覆盖。"""
    if type(claim_generation) is not int or claim_generation < 1:
        raise _domain("report_claim_invalid", "报表 claim 代数无效")
    if not isinstance(page, ReportPage):
        raise _domain("report_page_invalid", "报表页类型无效")
    run = session.exec(
        select(ReportSyncRun)
        .where(ReportSyncRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if run is None:
        raise _domain("report_run_not_found", "报表同步运行不存在")
    query, contract = _validated_query(run)
    if run.published_version is not None or run.status in {
        "COMPLETE",
        "FAILED",
        "CANCELLED",
        "STALE",
    }:
        raise _domain("report_run_closed", "报表同步运行已经结束")
    if page.page < 1:
        raise _domain("report_page_invalid", "报表页码无效")
    if claim_generation > run.claim_generation:
        raise _domain("report_claim_lost", "报表同步 claim 已被替换")
    existing = session.get(
        ReportStagedPage, (run_id, page.page), populate_existing=True
    )
    if claim_generation < run.claim_generation:
        if existing is None or not _same_page(existing, page, claim_generation, require_claim=False):
            raise _domain("report_claim_lost", "旧报表 worker 不能追加暂存页")
        return
    for item in page.rows:
        if any(not isinstance(value, str) for value in item.subject_key):
            raise _domain("report_scope_mismatch", "报表事实身份无效")
    if existing is not None:
        if _same_page(existing, page, claim_generation):
            return
        raise _domain("report_page_conflict", "同一报表页已有不同暂存证据")
    terminal = session.exec(
        select(ReportStagedPage)
        .where(ReportStagedPage.run_id == run_id, ReportStagedPage.complete.is_(True))
        .with_for_update()
    ).first()
    if terminal is not None and (page.page > terminal.page or page.complete):
        raise _domain("report_page_after_terminal", "报表完整页之后不能继续写页")
    staged = ReportStagedPage(
        run_id=run.id,
        page=page.page,
        tenant_id=run.tenant_id,
        advertiser_id=run.advertiser_id,
        claim_generation=claim_generation,
        next_page=page.next_page,
        complete=page.complete,
        evidence=_evidence_json(page),
        rows=[_row_payload(item) for item in page.rows],
        task_id=run.task_id,
    )
    _row_records([staged], query, contract)
    session.add(staged)
    session.flush()


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise _domain("report_value_invalid", "报表指标不是有效定点数") from exc
    else:
        raise _domain("report_value_invalid", "报表指标必须使用定点数字符串")
    if not result.is_finite():
        raise _domain("report_value_invalid", "报表指标不能是 NaN 或无穷值")
    return result


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise _domain("report_row_invalid", "报表桶时间无效")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _domain("report_row_invalid", "报表桶时间无效") from exc
    if result.tzinfo is None:
        raise _domain("report_row_invalid", "报表桶时间必须带时区")
    return result.astimezone(UTC)


def _attributes(value: Any) -> dict[str, Any]:
    """只留下已核验的描述/关联字段，绝不保存原始响应或签名下载地址。"""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _ATTRIBUTE_KEYS:
            continue
        encoded = _json_value(item)
        if _contains_url(encoded):
            continue
        result[str(key)] = encoded
    return result


def _contains_url(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized.startswith(("http://", "https://", "www."))
    if isinstance(value, dict):
        return any(_contains_url(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_url(item) for item in value)
    return False


def _query_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _run_coordinates(run: ReportSyncRun) -> dict[str, Any]:
    query = run.query
    required = (
        "report_contract",
        "metric_family",
        "granularity",
        "currency",
        "timezone",
        "attribution",
    )
    if any(
        not isinstance(query.get(item), str) or not query[item] for item in required
    ):
        raise _domain("report_query_invalid", "报表查询口径不完整")
    return {
        "tenant_id": run.tenant_id,
        "advertiser_id": run.advertiser_id,
        "granularity": query["granularity"],
        "report_contract": query["report_contract"],
        "metric_family": query["metric_family"],
        "currency": query["currency"],
        "timezone": query["timezone"],
        "attribution": query["attribution"],
    }


def _validated_query(run: ReportSyncRun):
    """发布前重新验证完整查询；不能因坏时区而回退到 UTC。"""
    try:
        query = decode_query(run.query)
        if query.advertiser_id != run.advertiser_id:
            raise ValueError("query advertiser differs from run")
        if report_partition_key(query) != run.partition_key:
            raise ValueError("partition key does not match query")
        contract = validate_query(
            query,
            channel=run.channel,
            ad_type=run.query.get("ad_type"),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise _domain("report_query_invalid", "报表查询合同无效") from exc
    return query, contract


def _newer_published_run(session: Session, run: ReportSyncRun) -> bool:
    if run.request_sequence is None:
        raise _domain("report_sequence_missing", "报表运行尚未取得数据库请求序号")
    newer = session.exec(
        select(ReportSyncRun.id)
        .where(
            ReportSyncRun.tenant_id == run.tenant_id,
            ReportSyncRun.advertiser_id == run.advertiser_id,
            ReportSyncRun.partition_key == run.partition_key,
            ReportSyncRun.request_sequence > run.request_sequence,
            ReportSyncRun.published_version.is_not(None),
        )
        .limit(1)
    ).first()
    return newer is not None


_AVAILABILITY = frozenset({"AVAILABLE", "MISSING", "UNAVAILABLE", "UNSUPPORTED", "FAILED"})


def _valid_subject(subject: Any, query: Any, contract: Any) -> bool:
    if type(subject) is not list or any(type(item) is not str or not item for item in subject):
        return False
    if contract.subject_kind != "material":
        return (
            len(subject) == 2
            and subject[0] == contract.subject_kind
        )
    if query.report_contract == "material_overview":
        return (
            len(subject) == 4
            and subject[:2] == ["material", query.dimensions[0]]
            and all(subject[2:])
            and (not query.filter_ids or subject[1] in query.filter_ids)
        )
    return (
        len(subject) == 4
        and subject[:2] == ["material", "main_material_id"]
        and all(subject[2:])
        and (not query.filter_ids or subject[2] in query.filter_ids)
    )


def _row_records(
    pages: list[ReportStagedPage], query: Any, contract: Any
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    metrics = tuple(query.metrics)
    for page in pages:
        for raw in page.rows:
            if not isinstance(raw, dict):
                raise _domain("report_row_invalid", "报表行不是对象")
            subject = raw.get("subject_key")
            values = raw.get("values")
            availability = raw.get("availability") or {}
            if not _valid_subject(subject, query, contract):
                raise _domain("report_row_invalid", "报表行身份无效")
            if not isinstance(values, dict) or not isinstance(availability, dict):
                raise _domain("report_row_invalid", "报表行指标结构无效")
            start = _parse_datetime(raw.get("bucket_start"))
            end = _parse_datetime(raw.get("bucket_end"))
            if start >= end:
                raise _domain("report_row_invalid", "报表桶范围无效")
            attrs = _attributes(raw.get("attributes", {}))
            for metric_name in set(values) | set(availability):
                amount = values.get(metric_name)
                if not isinstance(metric_name, str) or not metric_name:
                    raise _domain("report_row_invalid", "报表指标名称无效")
                if metric_name not in metrics:
                    continue
                if not supports_metric(
                    report_contract=query.report_contract,
                    metric_name=metric_name,
                ):
                    continue
                state = availability.get(metric_name, "AVAILABLE" if amount is not None else "MISSING")
                if state not in _AVAILABILITY:
                    raise _domain("report_availability_invalid", "报表可用性状态无效")
                if (state == "AVAILABLE") != (amount is not None):
                    raise _domain("report_availability_invalid", "报表值与可用性状态不一致")
                records.append(
                    {
                        "subject_key": subject,
                        "bucket_start": start,
                        "bucket_end": end,
                        "metric_name": metric_name,
                        "value": _parse_decimal(amount),
                        "availability": state,
                        "attributes": attrs,
                    }
                )
    return records


def publish_report(session: Session, *, run_id: UUID, claim_generation: int) -> int:
    """完整发布一个报表分片，返回本次原子发布版本。"""
    if type(claim_generation) is not int or claim_generation < 1:
        raise _domain("report_claim_invalid", "报表 claim 代数无效")
    run = session.exec(
        select(ReportSyncRun)
        .where(ReportSyncRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if run is None:
        raise _domain("report_run_not_found", "报表同步运行不存在")
    if run.claim_generation != claim_generation:
        raise _domain("report_claim_lost", "报表同步 claim 已被替换")
    if run.published_version is not None or run.status in {"FAILED", "CANCELLED", "STALE"}:
        raise _domain("report_run_closed", "报表同步运行已经结束")
    query, contract = _validated_query(run)
    route = _route_for_run(run)
    _ensure_route_authority(session, run, route)
    pages = list(
        session.exec(
            select(ReportStagedPage)
            .where(ReportStagedPage.run_id == run.id)
            .order_by(ReportStagedPage.page)
        ).all()
    )
    ordered = _complete_chain(pages)
    records = _row_records(ordered, query, contract)
    # 锁定账户行，使同一账户的 ReportFact/ReportCoverage 版本分配单调。
    account = session.exec(
        select(AdvertiserAccount)
        .where(
            AdvertiserAccount.tenant_id == run.tenant_id,
            AdvertiserAccount.advertiser_id == run.advertiser_id,
        )
        .with_for_update()
    ).one_or_none()
    if account is None:
        raise _domain("account_not_found", "报表账户不存在")
    # 版本栅栏必须在账户行锁之后检查；否则较新的发布者可能在检查后提交。
    if _newer_published_run(session, run):
        raise _domain("report_superseded", "较旧报表任务不能覆盖新版本")
    current_fact = session.exec(
        select(func.max(ReportFact.published_version)).where(
            ReportFact.tenant_id == run.tenant_id,
            ReportFact.advertiser_id == run.advertiser_id,
        )
    ).one()
    current_coverage = session.exec(
        select(func.max(ReportCoverage.published_version)).where(
            ReportCoverage.tenant_id == run.tenant_id,
            ReportCoverage.advertiser_id == run.advertiser_id,
        )
    ).one()
    version = max(current_fact or 0, current_coverage or 0) + 1
    coordinates = _run_coordinates(run)
    bucket_keys = set(report_buckets(query))
    present_by_subject_bucket: dict[tuple[tuple[str, ...], tuple[datetime, datetime]], set[str]] = {}
    for item in records:
        identity = (
            tuple(item["subject_key"]),
            (item["bucket_start"], item["bucket_end"]),
        )
        present_by_subject_bucket.setdefault(identity, set()).add(item["metric_name"])
    # 非空响应如果缺少请求指标，不能删除旧的完整指标组；适配器应显式补出不可用状态。
    if any(set(query.metrics) - metric_names for metric_names in present_by_subject_bucket.values()):
        raise _domain("report_metrics_incomplete", "报表页缺少请求指标，不能替换完整事实组")
    # 完整分片按指标组替换，只触碰本 partition_key；其他指标组保留。
    for bucket_start, bucket_end in bucket_keys:
        session.execute(
            delete(ReportFact).where(
                ReportFact.tenant_id == run.tenant_id,
                ReportFact.advertiser_id == run.advertiser_id,
                ReportFact.source_partition_key == run.partition_key,
                ReportFact.bucket_start == bucket_start,
                ReportFact.bucket_end == bucket_end,
                ReportFact.request_sequence <= run.request_sequence,
            )
        )
    for item in records:
        existing = session.exec(
            select(ReportFact)
            .where(
                ReportFact.tenant_id == run.tenant_id,
                ReportFact.advertiser_id == run.advertiser_id,
                ReportFact.subject_key == item["subject_key"],
                ReportFact.bucket_start == item["bucket_start"],
                ReportFact.bucket_end == item["bucket_end"],
                ReportFact.granularity == coordinates["granularity"],
                ReportFact.report_contract == coordinates["report_contract"],
                ReportFact.metric_family == coordinates["metric_family"],
                ReportFact.currency == coordinates["currency"],
                ReportFact.timezone == coordinates["timezone"],
                ReportFact.attribution == coordinates["attribution"],
                ReportFact.metric_name == item["metric_name"],
            )
            .with_for_update()
        ).one_or_none()
        if existing is not None and existing.request_sequence > run.request_sequence:
            # 重叠查询可能使用不同分片摘要；较新的事实仍然拥有唯一身份。
            continue
        if existing is None:
            existing = ReportFact(
                **coordinates,
                subject_key=item["subject_key"],
                bucket_start=item["bucket_start"],
                bucket_end=item["bucket_end"],
                metric_name=item["metric_name"],
            )
            session.add(existing)
        existing.value = item["value"]
        existing.availability = item["availability"]
        existing.attributes = item["attributes"]
        existing.published_version = version
        existing.request_sequence = run.request_sequence
        existing.source_partition_key = run.partition_key
        existing.source_run_id = run.id
        existing.observed_at = datetime.now(UTC)
    for bucket_start, bucket_end in bucket_keys:
        coverage = session.exec(
            select(ReportCoverage)
            .where(
                ReportCoverage.tenant_id == run.tenant_id,
                ReportCoverage.advertiser_id == run.advertiser_id,
                ReportCoverage.partition_key == run.partition_key,
                ReportCoverage.bucket_start == bucket_start,
                ReportCoverage.bucket_end == bucket_end,
            )
            .with_for_update()
        ).one_or_none()
        if coverage is None:
            coverage = ReportCoverage(
                **coordinates,
                bucket_start=bucket_start,
                bucket_end=bucket_end,
                partition_key=run.partition_key,
                filter_ids=list(query.filter_ids),
                requested_metrics=list(query.metrics),
                dimensions=list(query.dimensions),
                status="COMPLETE_EMPTY" if not records else "COMPLETE",
                missing_reason=None,
                observed_at=datetime.now(UTC),
                published_version=version,
                request_sequence=run.request_sequence,
                source_run_id=run.id,
            )
            session.add(coverage)
        elif coverage.request_sequence <= run.request_sequence:
            coverage.filter_ids = list(query.filter_ids)
            coverage.requested_metrics = list(query.metrics)
            coverage.dimensions = list(query.dimensions)
            coverage.status = "COMPLETE_EMPTY" if not records else "COMPLETE"
            coverage.missing_reason = None
            coverage.observed_at = datetime.now(UTC)
            coverage.published_version = version
            coverage.request_sequence = run.request_sequence
            coverage.source_run_id = run.id
    run.status = "COMPLETE"
    run.coverage = "COMPLETE_EMPTY" if not records else "COMPLETE"
    run.observed_at = datetime.now(UTC)
    run.completed_at = run.observed_at
    run.published_version = version
    session.flush()
    return version


def _compatible_coordinates(
    previous: ReportObservation, current: ReportObservation
) -> bool:
    return (
        previous.tenant_id == current.tenant_id
        and previous.advertiser_id == current.advertiser_id
        and previous.subject_kind == current.subject_kind
        and previous.subject_key == current.subject_key
        and previous.bucket_start == current.bucket_start
        and previous.bucket_end == current.bucket_end
        and previous.granularity == current.granularity
        and previous.report_contract == current.report_contract
        and previous.metric_family == current.metric_family
        and previous.currency == current.currency
        and previous.timezone == current.timezone
        and previous.attribution == current.attribution
        and previous.membership_digest == current.membership_digest
        and previous.grouping_revision == current.grouping_revision
    )


def observation_delta(
    previous: ReportObservation, current: ReportObservation
) -> dict[str, Decimal] | None:
    """只比较完全兼容的账户/系列同日观测；备注改名不改变 grouping_revision。"""
    if not isinstance(previous, ReportObservation) or not isinstance(
        current, ReportObservation
    ):
        raise TypeError("observation_delta requires ReportObservation")
    if not _compatible_coordinates(previous, current):
        return None
    if current.observed_at <= previous.observed_at:
        return None
    result: dict[str, Decimal] = {}
    for metric_name in set(previous.values).intersection(current.values):
        if (
            previous.availability.get(metric_name, "AVAILABLE") != "AVAILABLE"
            or current.availability.get(metric_name, "AVAILABLE") != "AVAILABLE"
        ):
            continue
        old = _parse_decimal(previous.values.get(metric_name))
        new = _parse_decimal(current.values.get(metric_name))
        # 缺失/不可用不是零，无法构造有意义的差值就跳过该指标。
        if old is None or new is None:
            continue
        result[metric_name] = new - old
    return result
