"""报表同步任务：一次请求（含全部分页）一行。

- 领取按租户轮转、按优先级与到期时间排序，大租户不能挤占小租户。
- 一次执行内读完全部分页再整份发布；超时或中断则下次从第 1 页重来，绝不发布半份。
- 额度/限流延后不计重试次数；读取错误最多 5 次指数退避；合同错误立即失败。
- 平台单次最多 2 万行，触顶即视为可能截断：先减少合并账户，再按系列 30 个一批。
"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage, ReportReader
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting import publish
from app.modules.reporting.store_models import ReportSyncJob

PRIORITY = {"hot": 1, "manual": 1, "material": 2, "daily": 3, "history": 4}
TRUNCATION_ROWS = 20_000
MAX_PROVIDER_RETRIES = 5
LEASE = timedelta(seconds=240)
_CONTRACT_ERRORS = frozenset(
    {
        "report_query_invalid",
        "report_response_invalid",
        "report_row_invalid",
        "report_value_invalid",
        "report_operation_invalid",
    }
)
_DEFERRED_CODES = frozenset(
    {
        "admission_deferred",
        "admission_unavailable",
        "tiktok_local_resources_unavailable",
        "gateway_credentials_changed",
        "read_deadline_exceeded",
    }
)


def _key(tenant_id, kind, tier, advertiser_ids, start, end, campaign_ids) -> str:
    payload = json.dumps(
        [
            str(tenant_id),
            kind,
            tier,
            sorted(advertiser_ids),
            start.isoformat(),
            end.isoformat(),
            sorted(campaign_ids),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def enqueue_job(
    session: Session,
    *,
    tenant_id,
    bc_id,
    kind,
    tier,
    advertiser_ids,
    start,
    end,
    frozen_route,
    actor_id,
    campaign_ids=(),
    due_at=None,
) -> UUID | None:
    """同一租户相同 (类型, 档位, 账户, 日期段, 系列) 未结束时不重复入队；返回新任务 ID 或 None。"""
    job_id = uuid4()
    row = (
        session.connection()
        .execute(
            text("""
        INSERT INTO report_sync_job (id, tenant_id, bc_id, kind, priority, status, advertiser_ids,
            campaign_ids, start_date, end_date, job_key, frozen_route, actor_id, due_at, attempts,
            request_count, created_at)
        VALUES (:id, :tenant, :bc, :kind, :priority, 'QUEUED', CAST(:advs AS jsonb), CAST(:camps AS jsonb),
            :start, :end, :key, CAST(:route AS jsonb), :actor, :due, 0, 0, now())
        ON CONFLICT (job_key) WHERE status IN ('QUEUED','RUNNING','WAITING') DO NOTHING
        RETURNING id"""),
            parameters={
                "id": job_id,
                "tenant": tenant_id,
                "bc": bc_id,
                "kind": kind,
                "priority": PRIORITY[tier],
                "advs": json.dumps(list(advertiser_ids)),
                "camps": json.dumps(list(campaign_ids)),
                "start": start,
                "end": end,
                "key": _key(
                    tenant_id, kind, tier, advertiser_ids, start, end, campaign_ids
                ),
                "route": json.dumps(frozen_route),
                "actor": actor_id,
                "due": due_at or datetime.now(UTC),
            },
        )
        .first()
    )
    return row[0] if row else None


def claim_jobs(session: Session, *, now: datetime, limit: int) -> list[ReportSyncJob]:
    """按租户轮转领取：每个租户内按 (优先级, 到期) 排名，先取各租户第 1 名，再取第 2 名……"""
    ids = (
        session.connection()
        .execute(
            text("""
        WITH ranked AS (
            SELECT id, priority, due_at,
                   ROW_NUMBER() OVER (PARTITION BY tenant_id ORDER BY priority, due_at, id) AS rn
            FROM report_sync_job
            WHERE (status IN ('QUEUED','WAITING') AND due_at <= :now
              AND (claimed_until IS NULL OR claimed_until <= :now))
               OR (status = 'RUNNING' AND claimed_until <= :now))
        SELECT id FROM ranked ORDER BY rn, priority, due_at, id LIMIT :limit"""),
            parameters={"now": now, "limit": limit},
        )
        .scalars()
        .all()
    )
    claimed = []
    for job_id in ids:
        job = session.exec(
            select(ReportSyncJob)
            .where(ReportSyncJob.id == job_id)
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if job is None or job.status not in ("QUEUED", "WAITING", "RUNNING"):
            continue
        if job.claimed_until is not None and job.claimed_until > now:
            continue
        job.status, job.claim_token, job.claimed_until = "RUNNING", uuid4(), now + LEASE
        claimed.append(job)
    session.flush()
    return claimed


def _accounts(session, tenant_id, advertiser_ids) -> dict[str, publish.AccountMeta]:
    rows = session.exec(
        select(AdvertiserAccount).where(
            AdvertiserAccount.tenant_id == tenant_id,
            col(AdvertiserAccount.advertiser_id).in_(advertiser_ids),
        )
    ).all()
    found = {
        r.advertiser_id: publish.AccountMeta(r.currency, r.timezone)
        for r in rows
        if r.currency and r.timezone
    }
    if set(found) != set(advertiser_ids):
        raise DomainError("account_metadata_incomplete", "广告账户缺少币种或时区")
    return found


def _read_all(read: Callable[[int], RawReportPage]) -> list[dict]:
    rows, page = [], 1
    while True:
        result = read(page)
        if result.total_number >= TRUNCATION_ROWS:
            raise DomainError(
                "report_truncation_risk", "报表行数达到平台上限，可能被截断"
            )
        rows.extend(result.rows)
        if result.next_page is None:
            return rows
        page = result.next_page


def _split(session, job: ReportSyncJob) -> bool:
    """多账户截断拆成独立账户；单账户在一次事务内读完所有系列批再发布。"""
    common = {
        "tenant_id": job.tenant_id,
        "bc_id": job.bc_id,
        "kind": job.kind,
        "tier": next(t for t, p in PRIORITY.items() if p == job.priority),
        "start": job.start_date,
        "end": job.end_date,
        "frozen_route": job.frozen_route,
        "actor_id": job.actor_id,
    }
    if len(job.advertiser_ids) > 1:
        for advertiser in job.advertiser_ids:
            enqueue_job(session, advertiser_ids=[advertiser], **common)
        return True
    return False


def _run(session, job: ReportSyncJob, reader: _CountingReader) -> int:
    advertisers = tuple(job.advertiser_ids)
    accounts = _accounts(session, job.tenant_id, advertisers)
    s, e, t, j = job.start_date, job.end_date, job.tenant_id, job.id
    if job.kind == "ad_day":
        try:
            if job.error_code == "report_timeout" and job.attempts:
                raise DomainError("report_truncation_risk", "超时后缩小读取范围")
            rows = _read_all(
                lambda p: reader.ad_day(
                    advertiser_ids=advertisers,
                    start=s,
                    end=e,
                    page=p,
                    campaign_ids=tuple(job.campaign_ids),
                )
            )
        except DomainError as error:
            if (
                error.code != "report_truncation_risk"
                or len(advertisers) != 1
                or job.campaign_ids
            ):
                raise
            rows = _read_campaign_batches(reader, advertisers, s, e)
        return publish.publish_ad_day(
            session,
            tenant_id=t,
            job_id=j,
            accounts=accounts,
            start=s,
            end=e,
            rows=rows,
            campaign_ids=tuple(job.campaign_ids),
        )
    if job.kind == "campaign_hour":
        rows = _read_all(
            lambda p: reader.campaign_hour(advertiser_ids=advertisers, day=s, page=p)
        )
        return publish.publish_campaign_hour(
            session, tenant_id=t, job_id=j, accounts=accounts, day=s, rows=rows
        )
    if job.kind == "campaign_day_utc":
        rows = _read_all(
            lambda p: reader.campaign_day_utc(
                advertiser_ids=advertisers, start=s, end=e, page=p
            )
        )
        return publish.publish_campaign_day_utc(
            session, tenant_id=t, job_id=j, accounts=accounts, start=s, end=e, rows=rows
        )
    if job.kind == "material_day":
        rows = _read_all(
            lambda p: reader.material_day(
                advertiser_id=advertisers[0], start=s, end=e, page=p
            )
        )
        return publish.publish_material_day(
            session, tenant_id=t, job_id=j, accounts=accounts, start=s, end=e, rows=rows
        )
    if job.kind == "account_day":
        rows = _read_all(
            lambda p: reader.account_day(
                advertiser_ids=advertisers, start=s, end=e, page=p
            )
        )
        publish.publish_account_reconciliation(
            session, tenant_id=t, accounts=accounts, start=s, end=e, rows=rows
        )
        return len(rows)
    raise DomainError("report_job_kind_invalid", "未知报表任务类型")


def _read_campaign_batches(reader, advertisers, start, end):
    """每次拆分都重新读取本期完整系列清单，缓存可能缺少新系列，不能作为过滤依据。"""
    try:
        campaigns = _read_all(
            lambda p: reader.campaign_day(
                advertiser_ids=advertisers, start=start, end=end, page=p
            )
        )
        ids = sorted(
            {str(row.get("dimensions", {}).get("campaign_id", "")) for row in campaigns}
        )
        if not ids or "" in ids or "None" in ids:
            raise DomainError("report_truncation_risk", "截断报表没有完整系列清单")
        rows = []
        for offset in range(0, len(ids), 30):
            batch = tuple(ids[offset : offset + 30])
            rows.extend(
                _read_all(
                    lambda p, batch=batch: reader.ad_day(
                        advertiser_ids=advertisers,
                        start=start,
                        end=end,
                        page=p,
                        campaign_ids=batch,
                    )
                )
            )
        return rows
    except DomainError as error:
        if error.code == "read_deadline_exceeded":
            # 已进入拆分读取的截止期限耗尽属于读取失败，不能无限走额度延后。
            raise DomainError("report_timeout", "系列分批读取超时") from error
        raise


class _CountingReader:
    def __init__(self, reader):
        self.reader = reader
        self.count = 0

    def __getattr__(self, name) -> Callable[..., RawReportPage]:
        read = getattr(self.reader, name)

        def counted(**kwargs):
            before = getattr(self.reader, "request_count", None)
            try:
                return read(**kwargs)
            finally:
                after = getattr(self.reader, "request_count", None)
                # 官方读取器提供物理发送计数；纯内存测试读取器每次调用即一次合成响应。
                self.count += (
                    after - before
                    if isinstance(before, int) and isinstance(after, int)
                    else 1
                )

        return counted


def execute_job(
    session_factory: Callable[[], Session],
    *,
    job_id: UUID,
    claim_token: UUID,
    reader: ReportReader,
) -> str:
    with session_factory() as session:
        job = session.exec(
            select(ReportSyncJob).where(ReportSyncJob.id == job_id).with_for_update()
        ).one()
        if job.claim_token != claim_token or job.status != "RUNNING":
            return "FAILED"  # 旧领取者：不得改写新领取的任务
        counted = _CountingReader(reader)
        try:
            # 按账户排序取得事务锁；不同档位和重叠日期必须从读取开始串行。
            # 账户对账与广告日共享锁，避免对账读取旧广告而覆盖新结果。
            lock_kind = "ad_day" if job.kind == "account_day" else job.kind
            for advertiser in sorted(job.advertiser_ids):
                acquired = (
                    session.connection()
                    .execute(
                        text(
                            "SELECT pg_try_advisory_xact_lock(hashtextextended(:scope, 0))"
                        ),
                        {"scope": f"report:{job.tenant_id}:{advertiser}:{lock_kind}"},
                    )
                    .scalar_one()
                )
                if not acquired:
                    raise AccountAdmissionDeferred(5000)
            _run(session, job, counted)
            job.status, job.error_code, job.finished_at = (
                "COMPLETE",
                None,
                datetime.now(UTC),
            )
            result = "COMPLETE"
        except AccountAdmissionDeferred as deferred:
            session.rollback()
            job = session.get(ReportSyncJob, job_id, with_for_update=True)
            if job is None or job.claim_token != claim_token or job.status != "RUNNING":
                return "FAILED"
            job.status, job.error_code = "WAITING", "admission_deferred"
            job.due_at = datetime.now(UTC) + timedelta(
                milliseconds=max(1000, deferred.retry_after_ms)
            )
            result = "WAITING"
        except DomainError as error:
            session.rollback()
            job = session.get(ReportSyncJob, job_id, with_for_update=True)
            if job is None or job.claim_token != claim_token or job.status != "RUNNING":
                return "FAILED"
            result = _handle_error(session, job, error)
        job.request_count += counted.count
        job.claim_token = job.claimed_until = None
        session.commit()
        return result


def _handle_error(session, job: ReportSyncJob, error: DomainError) -> str:
    job.error_code = error.code
    if error.code == "report_truncation_risk":
        if _split(session, job):
            job.status, job.finished_at = "COMPLETE", datetime.now(UTC)
            return "SPLIT"
    elif error.code in _DEFERRED_CODES:
        job.status, job.due_at = "WAITING", datetime.now(UTC) + timedelta(seconds=30)
        return "WAITING"
    elif error.code not in _CONTRACT_ERRORS and job.attempts < MAX_PROVIDER_RETRIES:
        job.attempts += 1
        job.status = "WAITING"
        job.due_at = datetime.now(UTC) + timedelta(
            seconds=min(1800, 60 * 2 ** (job.attempts - 1))
        )
        return "WAITING"
    job.status, job.finished_at = "FAILED", datetime.now(UTC)
    publish.mark_failed(
        session,
        tenant_id=job.tenant_id,
        advertiser_ids=job.advertiser_ids,
        kind=job.kind,
        start=job.start_date,
        end=job.end_date,
        error_code=error.code,
    )
    return "FAILED"
