"""报表同步任务：一次请求（含全部分页）一行。

- 领取按租户轮转、按优先级与到期时间排序，大租户不能挤占小租户。
- 广告完整读取后整份发布；素材按有界分页暂存和续传，完整后才原子发布。
- 额度/限流延后不计重试次数；读取错误最多 5 次指数退避；合同错误立即失败。
- 广告综合报表 2 万行触顶先拆账户/系列；素材拆分报表按分页证据续读，大跨日范围拆日期。
"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, text
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage, ReportReader
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting import material_links, publish
from app.modules.reporting.directory_refresh import refresh_report_directory
from app.modules.reporting.lanes import live_predicate
from app.modules.reporting.metrics import material_metric_values, parse_local_day
from app.modules.reporting.store_models import ReportSyncJob, ReportSyncPage

PRIORITY = {"hot": 1, "manual": 1, "material": 2, "daily": 3, "history": 4}
TRUNCATION_ROWS = 20_000
MATERIAL_PAGES_PER_STEP = 5
MATERIAL_SPLIT_ROWS = 5_000
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
    }
)


def _key(
    tenant_id,
    kind,
    tier,
    advertiser_ids,
    start,
    end,
    campaign_ids,
    bc_id,
    actor_id,
    frozen_route,
) -> str:
    payload = json.dumps(
        [
            str(tenant_id),
            kind,
            tier,
            sorted(advertiser_ids),
            start.isoformat(),
            end.isoformat(),
            sorted(campaign_ids),
            bc_id,
            str(actor_id),
            frozen_route,
        ],
        separators=(",", ":"),
        sort_keys=True,
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
    requested_at: datetime | None = None,
) -> UUID | None:
    """同一租户相同 (类型, 档位, 账户, 日期段, 系列) 未结束时不重复入队；返回新任务 ID 或 None。"""
    if PRIORITY[tier] <= 2:
        # 覆盖查询与插入必须处于同一个范围事务锁；并发的 [1,2] / [2,3]
        # 生产者不能都在查询时看见空集合，再各自重复安排账户 2。
        coverage_key = _key(
            tenant_id,
            kind,
            "live-coverage",
            (),
            start,
            end,
            campaign_ids,
            bc_id,
            actor_id,
            frozen_route,
        )
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": f"report-enqueue:{coverage_key}"},
        )
        # 相邻周期的批次边界可能变化；按实际账户与冻结范围合并正在执行的覆盖，
        # 不能只按五账户数组生成新键，否则新增一个账户就会再复制整轮热任务。
        pending = session.exec(
            select(ReportSyncJob.advertiser_ids).where(
                ReportSyncJob.tenant_id == tenant_id,
                ReportSyncJob.bc_id == bc_id,
                ReportSyncJob.actor_id == actor_id,
                ReportSyncJob.frozen_route == frozen_route,
                ReportSyncJob.kind == kind,
                ReportSyncJob.priority <= 2,
                ReportSyncJob.start_date == start,
                ReportSyncJob.end_date == end,
                ReportSyncJob.campaign_ids == list(campaign_ids),
                col(ReportSyncJob.status).in_(["QUEUED", "RUNNING", "WAITING"]),
            )
        ).all()
        covered = {advertiser for group in pending for advertiser in group}
        advertiser_ids = [
            advertiser for advertiser in advertiser_ids if advertiser not in covered
        ]
        if not advertiser_ids:
            return None
    job_id = uuid4()
    row = (
        session.connection()
        .execute(
            text("""
        INSERT INTO report_sync_job (id, tenant_id, bc_id, kind, priority, status, advertiser_ids,
            campaign_ids, start_date, end_date, job_key, frozen_route, actor_id, due_at, attempts,
            request_count, progress, created_at)
        VALUES (:id, :tenant, :bc, :kind, :priority, 'QUEUED', CAST(:advs AS jsonb), CAST(:camps AS jsonb),
            :start, :end, :key, CAST(:route AS jsonb), :actor, :due, 0, 0, CAST(:progress AS jsonb), clock_timestamp())
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
                    tenant_id,
                    kind,
                    tier,
                    advertiser_ids,
                    start,
                    end,
                    campaign_ids,
                    bc_id,
                    actor_id,
                    frozen_route,
                ),
                "route": json.dumps(frozen_route),
                "actor": actor_id,
                "due": due_at or datetime.now(UTC),
                "progress": json.dumps(
                    {"requested_at": requested_at.isoformat()} if requested_at else {}
                ),
            },
        )
        .first()
    )
    return row[0] if row else None


def claim_jobs(
    session: Session, *, now: datetime, limit: int, lane: str | None = None
) -> list[ReportSyncJob]:
    """按持久游标轮转租户，租户内部按优先级/到期排序；单槽补位也不能饿死其他租户。"""
    if lane not in {None, "live", "backfill"}:
        raise ValueError("invalid report lane")
    predicate = live_predicate("j")
    lane_filter = (
        "" if lane is None else f"AND {'' if lane == 'live' else 'NOT '}{predicate}"
    )
    # 实时金额与关联都按先到先服务，不能持续被较新广告任务插队。
    # 历史等待满一天后提升到每日修正的优先级，仍保留跨租户持久轮转。
    priority = (
        "CASE WHEN j.created_at <= :now - interval '24 hours' THEN LEAST(j.priority, 3) ELSE j.priority END"
        if lane == "backfill"
        else "j.priority"
    )
    local_order = (
        f"j.due_at, {priority}, j.id"
        if lane == "live"
        else f"{priority}, j.due_at, j.id"
    )
    global_order = "due_at, priority, id" if lane == "live" else "priority, due_at, id"
    ids = (
        session.connection()
        .execute(
            text(f"""
        WITH ranked AS (
            SELECT j.id, {priority} AS priority, j.due_at, c.last_dispatched_at,
                   ROW_NUMBER() OVER (PARTITION BY j.tenant_id ORDER BY {local_order}) AS rn
            FROM report_sync_job j
            LEFT JOIN report_dispatch_cursor c ON c.tenant_id = j.tenant_id
            WHERE (j.status IN ('QUEUED','WAITING') AND j.due_at <= :now
              AND (j.claimed_until IS NULL OR j.claimed_until <= :now)
              OR j.status = 'RUNNING' AND (j.claimed_until IS NULL OR j.claimed_until <= :now))
              {lane_filter})
        SELECT id FROM ranked
        ORDER BY rn, last_dispatched_at NULLS FIRST, {global_order} LIMIT :limit"""),
            parameters={"now": now, "lane_now": now, "limit": limit},
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


def request_time(job: ReportSyncJob) -> datetime:
    """截断拆分继承原请求的水位，创建子任务不产生更晚的业务观察意图。"""
    return (
        datetime.fromisoformat(job.progress["requested_at"])
        if job.progress.get("requested_at")
        else job.created_at
    )


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
        "requested_at": request_time(job),
    }
    if len(job.advertiser_ids) > 1:
        # 父任务先退出活动覆盖，避免幂等合并把自己的单账户子任务吞掉。
        job.status, job.finished_at = "COMPLETE", datetime.now(UTC)
        session.flush()
        for advertiser in job.advertiser_ids:
            enqueue_job(session, advertiser_ids=[advertiser], **common)
        return True
    return False


def _material_scope(job):
    # 暂存严格绑定租户、账户、授权代数和日期；修改冻结范围不能续用旧页。
    key = _key(
        job.tenant_id,
        job.kind,
        str(job.priority),
        job.advertiser_ids,
        job.start_date,
        job.end_date,
        job.campaign_ids,
        job.bc_id,
        job.actor_id,
        job.frozen_route,
    )
    return hashlib.sha256(f"{key}:{request_time(job).isoformat()}".encode()).hexdigest()


def _validate_material_page(session, job, accounts, result, page):
    if (
        any(
            type(value) is not int
            for value in (result.page, result.total_page, result.total_number)
        )
        or result.page != page
        or result.total_page < 0
        or result.total_number < 0
        or (
            result.total_number == 0
            and (result.rows or result.total_page not in (0, 1) or page != 1)
        )
        or (result.total_number > 0 and (not result.rows or page > result.total_page))
    ):
        raise DomainError("report_response_invalid", "素材分页证据无效")
    keys = []
    for row in result.rows:
        publish._account(row, accounts)
        dims = row.get("dimensions") or {}
        day = publish._in_range(
            parse_local_day(dims.get("stat_time_day")), job.start_date, job.end_date
        )
        if not dims.get("main_material_id") or not dims.get("main_material_type"):
            raise DomainError("report_row_invalid", "素材行缺少主素材身份")
        material_metric_values(row.get("metrics") or {})
        keys.append(
            json.dumps(
                [day.isoformat(), str(dims["main_material_id"])], separators=(",", ":")
            )
        )
    if len(set(keys)) != len(keys):
        raise DomainError("report_row_invalid", "素材分页包含重复日期与主素材")
    if (
        keys
        and session.connection()
        .execute(
            text("""
        SELECT EXISTS(SELECT 1 FROM report_sync_page
          WHERE job_id=:job AND tenant_id=:tenant AND row_keys ?| CAST(:keys AS text[]))
    """),
            {"job": job.id, "tenant": job.tenant_id, "keys": keys},
        )
        .scalar_one()
    ):
        raise DomainError("report_row_invalid", "素材分页与已暂存页重复")
    return keys


def _split_material_dates(session, job, result: RawReportPage):
    days = (job.end_date - job.start_date).days + 1
    # 小账户保留批量请求；仅大范围缩小，最多七天且下一层仍可按首分页继续拆。
    chunk_days = max(1, min(7, days * MATERIAL_SPLIT_ROWS // result.total_number))
    job.progress = job.progress | {
        "material_split": {
            "total_number": result.total_number,
            "total_page": result.total_page,
            "chunk_days": chunk_days,
        }
    }
    job.status, job.finished_at = "COMPLETE", datetime.now(UTC)
    session.flush()
    session.connection().execute(
        delete(ReportSyncPage).where(col(ReportSyncPage.job_id) == job.id)
    )
    cursor = job.start_date
    while cursor <= job.end_date:
        end = min(job.end_date, cursor + timedelta(days=chunk_days - 1))
        enqueue_job(
            session,
            tenant_id=job.tenant_id,
            bc_id=job.bc_id,
            kind=job.kind,
            tier=next(
                tier for tier, priority in PRIORITY.items() if priority == job.priority
            ),
            advertiser_ids=job.advertiser_ids,
            start=cursor,
            end=end,
            frozen_route=job.frozen_route,
            actor_id=job.actor_id,
            requested_at=request_time(job),
        )
        cursor = end + timedelta(days=1)


def _run_material(session, job, reader, accounts):
    if len(accounts) != 1 or job.campaign_ids:
        raise DomainError("report_query_invalid", "素材日报要求一个账户且不能限定系列")
    scope = _material_scope(job)
    progress = job.progress.get("material_paging")
    if progress is None:
        progress = {"scope": scope, "next_page": 1}
    if (
        progress.get("scope") != scope
        or type(progress.get("next_page")) is not int
        or progress["next_page"] < 1
    ):
        raise DomainError("report_query_invalid", "素材暂存与冻结请求范围不一致")
    page = progress["next_page"]
    stored_pages = session.exec(
        select(ReportSyncPage.page)
        .where(col(ReportSyncPage.job_id) == job.id)
        .order_by(col(ReportSyncPage.page))
    ).all()
    if stored_pages != list(range(1, page)):
        raise DomainError("report_response_invalid", "素材暂存分页不连续")
    for _ in range(MATERIAL_PAGES_PER_STEP):
        result = reader.material_day(
            advertiser_id=job.advertiser_ids[0],
            start=job.start_date,
            end=job.end_date,
            page=page,
        )
        keys = _validate_material_page(session, job, accounts, result, page)
        if (
            page == 1
            and result.total_number > MATERIAL_SPLIT_ROWS
            and job.start_date < job.end_date
        ):
            _split_material_dates(session, job, result)
            return 0
        if page > 1 and (
            result.total_page != progress.get("total_page")
            or result.total_number != progress.get("total_number")
        ):
            # 平台无快照 token，总数变化不能混接两套页。显式、有重试预算地重新读取。
            raise DomainError(
                "report_snapshot_changed", "素材分页总数变化，需要重建完整快照"
            )
        session.add(
            ReportSyncPage(
                job_id=job.id,
                tenant_id=job.tenant_id,
                page=page,
                scope_key=scope,
                rows=list(result.rows),
                row_keys=keys,
                total_page=result.total_page,
                total_number=result.total_number,
            )
        )
        progress = {
            "scope": scope,
            "next_page": page + 1,
            "total_page": result.total_page,
            "total_number": result.total_number,
        }
        job.progress = job.progress | {"material_paging": progress}
        session.flush()
        if result.next_page is None:
            staged = session.exec(
                select(ReportSyncPage)
                .where(col(ReportSyncPage.job_id) == job.id)
                .order_by(col(ReportSyncPage.page))
            ).all()
            if any(
                item.scope_key != scope or item.tenant_id != job.tenant_id
                for item in staged
            ):
                raise DomainError("report_query_invalid", "素材暂存证据超出冻结范围")
            rows = [row for item in staged for row in item.rows]
            if len(rows) != result.total_number:
                raise DomainError("report_response_invalid", "素材完整页数与行数不一致")
            count = publish.publish_material_day(
                session,
                tenant_id=job.tenant_id,
                job_id=job.id,
                accounts=accounts,
                start=job.start_date,
                end=job.end_date,
                rows=rows,
                requested_at=request_time(job),
            )
            enqueue_job(
                session,
                tenant_id=job.tenant_id,
                bc_id=job.bc_id,
                kind="material_links",
                tier=next(
                    tier
                    for tier, priority in PRIORITY.items()
                    if priority == job.priority
                ),
                advertiser_ids=job.advertiser_ids,
                start=job.start_date,
                end=job.end_date,
                frozen_route=job.frozen_route,
                actor_id=job.actor_id,
            )
            return count
        page = result.next_page
    # 只有完整页边界才让出账户锁；已提交页和游标在同一事务中持久化。
    return None


def cleanup_material_pages(session, *, now: datetime, retention_days: int = 7) -> int:
    """仅清理已终结七天的任务暂存；WAITING/RUNNING/QUEUED 均保留以便恢复。"""
    terminal = select(ReportSyncJob.id).where(
        col(ReportSyncJob.status).in_(["COMPLETE", "FAILED", "CANCELLED"]),
        col(ReportSyncJob.finished_at) < now - timedelta(days=retention_days),
    )
    result = session.connection().execute(
        delete(ReportSyncPage).where(col(ReportSyncPage.job_id).in_(terminal))
    )
    return result.rowcount


def _run(session, job: ReportSyncJob, reader: _CountingReader) -> int | None:
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
        count = publish.publish_ad_day(
            session,
            tenant_id=t,
            job_id=j,
            accounts=accounts,
            start=s,
            end=e,
            rows=rows,
            campaign_ids=tuple(job.campaign_ids),
            requested_at=request_time(job),
        )
        # 完整报表与补目录 outbox 同事务提交，新投放对象不等全量历史扫描。
        refresh_report_directory(session, job)
        return count
    if job.kind == "campaign_hour":
        rows = _read_all(
            lambda p: reader.campaign_hour(advertiser_ids=advertisers, day=s, page=p)
        )
        return publish.publish_campaign_hour(
            session,
            tenant_id=t,
            job_id=j,
            accounts=accounts,
            day=s,
            rows=rows,
            requested_at=request_time(job),
        )
    if job.kind == "material_day":
        return _run_material(session, job, reader, accounts)
    if job.kind == "material_links":
        complete = material_links.refresh_material_links(
            session,
            tenant_id=t,
            advertiser_id=advertisers[0],
            read_overview=lambda p: reader.material_ad_links(
                advertiser_id=advertisers[0], page=p
            ),
            read_ad_posts=lambda ads, first, last, p: reader.material_ad_posts(
                advertiser_id=advertisers[0], ad_ids=ads, start=first, end=last, page=p
            ),
            now=datetime.now(UTC),
            start=s,
            end=e,
            progress=job.progress,
        )
        # JSONB 的原地修改需要显式通知 ORM；每个续步都持久化已经完成的页和关联。
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(job, "progress")
        return None if not complete else 0
    if job.kind == "account_day":
        rows = _read_all(
            lambda p: reader.account_day(
                advertiser_ids=advertisers, start=s, end=e, page=p
            )
        )
        return publish.publish_account_reconciliation(
            session,
            tenant_id=t,
            accounts=accounts,
            start=s,
            end=e,
            rows=rows,
            requested_at=request_time(job),
        )
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
            more = _run(session, job, counted) is None
            if more:
                job.status, job.error_code = "WAITING", None
                job.due_at = datetime.now(UTC)
                result = "WAITING"
            else:
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
    if error.code == "report_snapshot_changed" and job.kind == "material_day":
        session.connection().execute(
            delete(ReportSyncPage).where(col(ReportSyncPage.job_id) == job.id)
        )
        job.progress = {
            key: value
            for key, value in job.progress.items()
            if key != "material_paging"
        }
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
        requested_at=request_time(job),
    )
    return "FAILED"
