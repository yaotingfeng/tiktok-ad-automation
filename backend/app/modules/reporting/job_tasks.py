"""报表后台入口：冻结授权、分档规划、持久派发与有限租约恢复。"""

import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

from billiard.exceptions import SoftTimeLimitExceeded
from redis import Redis
from sqlalchemy import case, delete, func, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit, validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.models import AdvertiserAccount
from app.modules.accounts.routing import verify_route
from app.modules.ads.directory_dispatch import (
    TARGETED_TASK_NAME,
    directory_step_key,
    enqueue_directory_step,
    is_targeted,
    targeted_capacity,
)
from app.modules.ads.directory_requests import (
    directory_ad_types,
    directory_due_accounts,
    request_directory,
)
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting import planner, publish
from app.modules.reporting.jobs import (
    LEASE,
    _CountingReader,
    _read_all,
    claim_jobs,
    cleanup_material_pages,
    execute_job,
    request_time,
)
from app.modules.reporting.lanes import lane_condition
from app.modules.reporting.metrics import parse_decimal
from app.modules.reporting.partitions import ensure_month_partitions
from app.modules.reporting.query_models import QuerySnapshot
from app.modules.reporting.store_models import (
    AdSyncBc,
    CampaignReportHour,
    DirectoryDispatchCursor,
    ReportBalanceRun,
    ReportDispatchCursor,
    ReportSyncJob,
)

logger = logging.getLogger(__name__)

# 每次派发最多入队的目录运行数；其余留给后续轮次，保证规划耗时有界。
DIRECTORY_DISPATCH_BATCH = 1000
# 全量目录只提前投递有界数量的运行，历史初始请求不再持续填满 broker。
# 抓取由单槽目录 Worker 与 10 并发实时目录 Worker 共同消费；单步约 2 秒而投递往返更长，
# 窗口 12 实测仅约 50 步/分钟、Worker 大半空闲，放到 32 让 11 个消费者持续有活。
# 发布仍单槽；超出调用准入的步骤由准入层延后，不会突破平台调用额度。
DIRECTORY_DISPATCH_WINDOW = 32
# 事实与关联独立限制在途任务；这是领取上限，不代表共享 Worker 中的专属执行槽。
# 12 个 prefork 配两批有界在途名额，以候补吸收 outbox 交付空档；名额不等于执行并发。
# 候补仍受领取后 240 秒租约约束；排队过期须重新领取，不能由旧消息直接续租。
REPORT_DISPATCH_WINDOW = 24
LINK_DISPATCH_WINDOW = 1
BACKFILL_DISPATCH_WINDOW = 1
# 目录消息发布后超过此时长仍未被领取，视为丢失并重新入队。
DIRECTORY_MESSAGE_LOST_AFTER = timedelta(hours=6)
TARGETED_MESSAGE_LOST_AFTER = timedelta(minutes=2)

register_dispatch_task("reporting.job_step", "ads-reporting")
register_dispatch_task("reporting.backfill_step", "ads-backfill")
register_dispatch_task("reporting.plan_due", "control")


def _context(row):
    return TenantContext(
        tenant_id=row.tenant_id, actor_id=row.actor_id, role="operator"
    )


@contextmanager
def _reader_for(*, database_engine, redis_client, plan, deadline):
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=_context(plan),
        route=FrozenTikTokRoute.model_validate(plan.frozen_route),
        task_deadline=deadline,
    ) as gateway:
        yield gateway.report_reader


def _active(session, reader, plan, accounts, now, *, days):
    # BC/账户筛选也按各账户本地日期分组，不能把 UTC 日期误作所有账户的今天。
    timezones = dict(
        session.exec(
            select(AdvertiserAccount.advertiser_id, AdvertiserAccount.timezone).where(
                AdvertiserAccount.tenant_id == plan.tenant_id,
                col(AdvertiserAccount.advertiser_id).in_(accounts),
            )
        ).all()
    )
    windows = {}
    for account, timezone in timezones.items():
        end = planner.local_today(timezone, now)
        windows.setdefault((end - timedelta(days=days - 1), end), []).append(account)
    active = set()
    if plan.bc_report_supported is not False:
        requests = [
            (start, end, tuple(sorted(candidates)))
            for (start, end), candidates in sorted(windows.items())
        ]
        fingerprint = sha256(
            repr((plan.frozen_route, str(plan.actor_id), requests)).encode()
        ).hexdigest()
        key = f"bc:{days}"
        progress: dict[str, Any] = plan.screening_progress.get(key, {})
        if progress.get("fingerprint") != fingerprint:
            progress = {
                "fingerprint": fingerprint,
                "offset": 0,
                "page": 1,
                "active": [],
            }
        active.update(progress["active"])
        for index in range(progress["offset"], len(requests)):
            start, end, candidates = requests[index]
            page = progress["page"] if index == progress["offset"] else 1
            while True:
                result = reader.bc_spend(
                    bc_id=plan.bc_id, start=start, end=end, page=page
                )
                for row in result.rows:
                    account = str((row.get("dimensions") or {}).get("advertiser_id"))
                    spend = parse_decimal((row.get("metrics") or {}).get("spend"))
                    if account in candidates and spend is not None and spend > 0:
                        active.add(account)
                plan.screening_progress = {
                    **plan.screening_progress,
                    key: {
                        "fingerprint": fingerprint,
                        "offset": index if result.next_page else index + 1,
                        "page": result.next_page or 1,
                        "active": sorted(active),
                    },
                }
                session.flush()
                if result.next_page is None:
                    break
                page = result.next_page
        plan.screening_progress = {
            k: v for k, v in plan.screening_progress.items() if k != key
        }
        if days == 1:
            plan.active_screened_at = now
        return sorted(active)
    requests = []
    for (start, end), candidates in sorted(windows.items()):
        candidates = sorted(candidates)
        for offset in range(0, len(candidates), 5):
            for s, e in planner._shards(start, end):
                requests.append((tuple(candidates[offset : offset + 5]), s, e))
    # 日期或冻结范围变化后重新筛选；同一轮失败只重读尚未完成的请求。
    fingerprint = sha256(
        repr((plan.frozen_route, str(plan.actor_id), requests)).encode()
    ).hexdigest()
    key = str(days)
    progress: dict[str, Any] = plan.screening_progress.get(key, {})
    if progress.get("fingerprint") != fingerprint:
        progress = {"fingerprint": fingerprint, "offset": 0, "active": []}
    active.update(progress["active"])
    for index in range(progress["offset"], len(requests)):
        batch, start, end = requests[index]
        rows = _read_all(
            lambda p, batch=batch, start=start, end=end: reader.account_day(
                advertiser_ids=batch, start=start, end=end, page=p
            )
        )
        for row in rows:
            account = str(row.get("dimensions", {}).get("advertiser_id", ""))
            spend = parse_decimal(row.get("metrics", {}).get("spend"))
            if account in batch and spend is not None and spend > 0:
                active.add(account)
        plan.screening_progress = {
            **plan.screening_progress,
            key: {
                "fingerprint": fingerprint,
                "offset": index + 1,
                "active": sorted(active),
            },
        }
        session.flush()
    plan.screening_progress = {
        k: v for k, v in plan.screening_progress.items() if k != key
    }
    if days == 1:
        plan.active_screened_at = now
    return sorted(active)


def _advance(due, now, interval):
    return due + (max(0, (now - due) // interval) + 1) * interval


def _message_lost(session: Session, row: AdDirectoryRun, now: datetime) -> bool:
    key = directory_step_key(row)
    published = session.exec(
        select(PendingDispatch.published_at).where(
            PendingDispatch.tenant_id == row.tenant_id,
            PendingDispatch.task_key == key,
        )
    ).first()
    threshold = (
        TARGETED_MESSAGE_LOST_AFTER
        if is_targeted(row)
        else DIRECTORY_MESSAGE_LOST_AFTER
    )
    return published is not None and published < now - threshold


def _dispatch_directories(session, now):
    # 本代数、本页的投递记录已存在（无论是否已发布）就不再重复入队：积压时
    # 若每轮重写全部运行，规划会长时间占住 control 唯一并发，使投递发布被饿死。
    targeted = (
        func.coalesce(func.jsonb_array_length(AdDirectoryRun.query["ids"]), 0) > 0
    ) | (
        func.coalesce(func.jsonb_array_length(AdDirectoryRun.query["parent_ids"]), 0)
        > 0
    )
    current_key = func.concat(
        case((targeted, TARGETED_TASK_NAME + ":"), else_="ads.sync_step:"),
        col(AdDirectoryRun.id),
        ":",
        col(AdDirectoryRun.claim_generation),
        ":page:",
        col(AdDirectoryRun.next_page),
    )
    enqueued = (
        select(col(PendingDispatch.id))
        .where(
            PendingDispatch.tenant_id == col(AdDirectoryRun.tenant_id),
            PendingDispatch.task_key == current_key,
        )
        .exists()
    )
    # 消息发布后长时间仍未被领取，视为在领取前丢失（如任务在加锁前失败）：
    # 换新代数重新入队。若旧消息其实仍在队列中，执行时代数不符会直接结束。
    lost = (
        select(col(PendingDispatch.id))
        .where(
            PendingDispatch.tenant_id == col(AdDirectoryRun.tenant_id),
            PendingDispatch.task_key == current_key,
            col(PendingDispatch.published_at)
            < case(
                (targeted, now - TARGETED_MESSAGE_LOST_AFTER),
                else_=now - DIRECTORY_MESSAGE_LOST_AFTER,
            ),
        )
        .exists()
    )
    # 并发唤醒须原子核对窗口；旧队列自然消费，不清队列或伪造完成。
    locked = (
        session.connection()
        .execute(
            text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:directory-dispatch-window', 0))"
            )
        )
        .scalar_one()
    )
    if not locked:
        return 0
    inflight = session.exec(
        select(func.count())
        .select_from(AdDirectoryRun)
        .where(
            col(AdDirectoryRun.status).in_(["QUEUED", "RUNNING", "WAITING_REMOTE"]),
            AdDirectoryRun.channel == "OFFICIAL_API",
            ~targeted,
            # 已有领取记录说明本代数消息已被消费；租约过期后不再算在途，否则未判丢失的
            # 6 小时内窗口被永久占满。与下方候选条件一致，过期运行会换代重新入队。
            (enqueued & ~lost & col(AdDirectoryRun.claimed_until).is_(None))
            | (col(AdDirectoryRun.claimed_until) > now),
        )
    ).one()
    available = max(0, DIRECTORY_DISPATCH_WINDOW - inflight)
    targeted_available = targeted_capacity(session, now)
    # 素材目录每次十余页，系列/组/广告目录只有一两页且决定止损父链与目录新鲜度；
    # 全量 lane 内对象目录优先，素材目录用剩余名额，避免旧素材积压饿死对象目录。
    creative_last = case((col(AdDirectoryRun.kind) == "creative", 1), else_=0)
    lane_ranked = (
        select(
            AdDirectoryRun.id,
            func.row_number()
            .over(
                partition_by=(targeted, col(AdDirectoryRun.tenant_id)),
                order_by=(
                    targeted.desc(),
                    creative_last,
                    col(AdDirectoryRun.created_at),
                    col(AdDirectoryRun.id),
                ),
            )
            .label("lane_rn"),
        ).where(
            (targeted & (targeted_available > 0)) | (~targeted & (available > 0)),
            col(AdDirectoryRun.status).in_(["QUEUED", "RUNNING", "WAITING_REMOTE"]),
            AdDirectoryRun.channel == "OFFICIAL_API",
            col(AdDirectoryRun.next_attempt_at).is_(None)
            | (col(AdDirectoryRun.next_attempt_at) <= now),
            # 租约过期的运行会进入新代数、生成新投递标识，仍需入队。
            col(AdDirectoryRun.claimed_until).is_(None) & (~enqueued | lost)
            | (col(AdDirectoryRun.claimed_until) <= now),
        )
    ).subquery()
    # 各lane先按真实空槽裁剪，再恢复原租户逐轮次序；小批量也不会先取同租户两类。
    ranked = (
        select(
            AdDirectoryRun.id,
            func.row_number()
            .over(
                partition_by=col(AdDirectoryRun.tenant_id),
                order_by=(
                    targeted.desc(),
                    creative_last,
                    col(AdDirectoryRun.created_at),
                    col(AdDirectoryRun.id),
                ),
            )
            .label("rn"),
        )
        .join(lane_ranked, lane_ranked.c.id == AdDirectoryRun.id)
        .where(
            lane_ranked.c.lane_rn
            <= case((targeted, targeted_available), else_=available)
        )
    ).subquery()
    rows = session.exec(
        select(AdDirectoryRun)
        .join(ranked, ranked.c.id == AdDirectoryRun.id)
        .outerjoin(
            DirectoryDispatchCursor,
            col(DirectoryDispatchCursor.tenant_id) == col(AdDirectoryRun.tenant_id),
        )
        .order_by(
            ranked.c.rn,
            col(DirectoryDispatchCursor.last_dispatched_at).asc().nulls_first(),
            targeted.desc(),
            creative_last,
            col(AdDirectoryRun.created_at),
            col(AdDirectoryRun.id),
        )
        .limit(DIRECTORY_DISPATCH_BATCH)
        .with_for_update(skip_locked=True, of=AdDirectoryRun)
    ).all()
    # 批量识别末页；全量发布保留独立队列，定向抓取/发布使用实时目录队列。
    terminal_runs = (
        set(
            session.exec(
                select(AdDirectoryPage.run_id).where(
                    col(AdDirectoryPage.run_id).in_([row.id for row in rows]),
                    col(AdDirectoryPage.complete).is_(True),
                )
            ).all()
        )
        if rows
        else set()
    )
    count = 0
    served = set()
    for row in rows:
        if is_targeted(row):
            if targeted_available <= 0:
                continue
            targeted_available -= 1
        else:
            if available <= 0:
                continue
            available -= 1
        if row.claimed_until is not None or _message_lost(session, row, now):
            row.claim_generation += 1
            row.claim_token = row.claimed_until = None
        enqueue_directory_step(session, row, publish_only=row.id in terminal_runs)
        served.add(row.tenant_id)
        count += 1
    for tenant in sorted(served):
        statement = insert(DirectoryDispatchCursor).values(
            tenant_id=tenant, last_dispatched_at=now
        )
        session.exec(
            statement.on_conflict_do_update(
                index_elements=["tenant_id"],
                set_={
                    "last_dispatched_at": func.greatest(
                        col(DirectoryDispatchCursor.last_dispatched_at),
                        statement.excluded.last_dispatched_at,
                    )
                },
            )
        )
    return count


def plan_due_runs(*, database_engine, redis_client, now=None):
    if not settings.ADS_SYNC_ENABLED:
        return 0
    now = now or datetime.now(UTC)
    allow_backfill = (
        planner.calls_today(redis_client, now)
        <= settings.TIKTOK_REPORT_DAILY_CALL_BUDGET * 0.7
    )
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    # 额度紧张优先让出历史补数，不静默把止损数据降成小时级。
    hot, material = planner.HOT_INTERVAL, planner.MATERIAL_INTERVAL
    with Session(database_engine) as session, session.begin():
        ensure_month_partitions(session, today=now.date())
        planner.ensure_bc_plans(session, now=now)
        keys = [(p.tenant_id, p.bc_id) for p in planner.due_plans(session, now=now)]
    dispatched = _dispatch_pending(database_engine, now, allow_backfill=allow_backfill)
    for key in keys:
        if datetime.now(UTC) >= deadline:
            break
        try:
            _plan_one(
                database_engine,
                redis_client,
                key,
                now,
                deadline,
                hot,
                material,
                allow_backfill=allow_backfill,
            )
        except SoftTimeLimitExceeded:
            # 已提交计划和派发不随后续慢请求回滚，下一轮从未完成计划继续。
            break
        except Exception:
            # 单个 BC 的意外错误（如数据约束失败）只推迟该 BC 并记录原因；
            # 否则它会回滚、到期时间不前进，每轮都排在最前面拖垮所有租户。
            logger.exception("report plan failed: tenant=%s bc=%s", *key)
            _defer_failed_plan(database_engine, key, now)
        dispatched += _dispatch_pending(
            database_engine, now, allow_backfill=allow_backfill
        )
    return dispatched


def _defer_failed_plan(
    database_engine: Any, key: tuple[UUID, str], now: datetime
) -> None:
    with Session(database_engine) as session, session.begin():
        plan = session.exec(
            select(AdSyncBc)
            .where(AdSyncBc.tenant_id == key[0], AdSyncBc.bc_id == key[1])
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if plan is None:
            return
        plan.disabled_reason = "plan_failed"
        _defer_plan(plan, now + timedelta(minutes=5))


def _defer_plan(plan, retry_at):
    # 所有已到期档位一起尊重读取/额度退避，不能被其他到期字段绕过。
    for field in (
        "next_hot_at",
        "next_daily_at",
        "next_material_at",
        "next_directory_at",
    ):
        setattr(plan, field, max(getattr(plan, field), retry_at))


def _plan_one(
    database_engine,
    redis_client,
    key,
    now,
    deadline,
    hot,
    material,
    *,
    allow_backfill=True,
):
    with Session(database_engine) as session, session.begin():
        plan = session.exec(
            select(AdSyncBc)
            .where(AdSyncBc.tenant_id == key[0], AdSyncBc.bc_id == key[1])
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if plan is None or not plan.enabled:
            return
        accounts = planner.owned_accounts(session, plan)
        if not accounts:
            return
        counted = None
        try:
            if plan.next_hot_at <= now or (
                allow_backfill and plan.next_daily_at <= now
            ):
                with _reader_for(
                    database_engine=database_engine,
                    redis_client=redis_client,
                    plan=plan,
                    # 控制面不等待整 BC 数百次外部请求；账户筛选游标逐批持久化，
                    # 8 秒到期后下一轮继续，让 outbox、监控和止损调度仍有执行机会。
                    deadline=min(deadline, datetime.now(UTC) + timedelta(seconds=8)),
                ) as reader:
                    if reader is None:
                        plan.enabled, plan.disabled_reason = (
                            False,
                            "api_binding_required",
                        )
                        return
                    counted = _CountingReader(reader)
                    if plan.next_hot_at <= now:
                        active = sorted(
                            set(_active(session, counted, plan, accounts, now, days=1))
                            | planner.known_reporting_accounts(
                                session, plan, accounts=accounts, now=now
                            )
                        )
                        planner.plan_hot(session, plan, active=active, now=now)
                        planner.mark_confirmed_empty(
                            session,
                            plan,
                            inactive=sorted(set(accounts) - set(active)),
                            window_fn=lambda tz: planner._hot_window(tz, now),
                            now=now,
                        )
                        plan.last_active_advertiser_ids = active
                        plan.next_hot_at = _advance(plan.next_hot_at, now, hot)
                    if allow_backfill and plan.next_daily_at <= now:
                        active = _active(session, counted, plan, accounts, now, days=32)
                        planner.plan_daily(session, plan, active_32d=active, now=now)
                        planner.mark_confirmed_empty(
                            session,
                            plan,
                            inactive=sorted(set(accounts) - set(active)),
                            window_fn=planner.daily_window(now),
                            now=now,
                        )
                        plan.next_daily_at = planner._next_daily(now)
            if plan.next_material_at <= now:
                planner.plan_material(
                    session, plan, active=plan.last_active_advertiser_ids, now=now
                )
                plan.next_material_at = _advance(plan.next_material_at, now, material)
            # 授权后的历史回补立即规划一次，不等到每日回刷；未知开户时间稍后重查。
            if allow_backfill:
                planner.plan_history(session, plan, now=now)
            if plan.next_directory_at <= now:
                # 目录扫描独立回滚：数据库超时不能撤销前面已经规划的实时任务。
                try:
                    with session.begin_nested():
                        previous_timeout = (
                            session.connection()
                            .execute(text("SHOW statement_timeout"))
                            .scalar_one()
                        )
                        session.connection().execute(
                            text("SELECT set_config('statement_timeout', '8s', true)")
                        )
                        # 全量目录（含已删除）已覆盖只读在投对象的需求，周期只发这一种；
                        # 无消耗账户按天刷新，未使用类型只探测广告系列。
                        due = directory_due_accounts(
                            session,
                            tenant_id=plan.tenant_id,
                            bc_id=plan.bc_id,
                            actor_id=plan.actor_id,
                            frozen_route=plan.frozen_route,
                            advertiser_ids=accounts,
                            active_ids=plan.last_active_advertiser_ids,
                            now=now,
                        )
                        request_directory(
                            session,
                            tenant_id=plan.tenant_id,
                            bc_id=plan.bc_id,
                            frozen_route=plan.frozen_route,
                            actor_id=plan.actor_id,
                            advertiser_ids=due,
                            scope="directory",
                            ad_types=directory_ad_types(
                                session,
                                tenant_id=plan.tenant_id,
                                advertiser_ids=due,
                                active_ids=plan.last_active_advertiser_ids,
                                now=now,
                            ),
                        )
                        plan.next_directory_at = _advance(
                            plan.next_directory_at, now, planner.DIRECTORY_INTERVAL
                        )
                        session.connection().execute(
                            text(
                                "SELECT set_config('statement_timeout', :value, true)"
                            ),
                            {"value": previous_timeout},
                        )
                except DBAPIError as error:
                    if getattr(error.orig, "sqlstate", None) != "57014":
                        raise
                    # SAVEPOINT 已恢复事务和原 timeout；只让全量目录稍后再规划。
                    plan.next_directory_at = now + timedelta(minutes=5)
                    logger.warning(
                        "directory planning query cancelled: tenant=%s bc=%s",
                        plan.tenant_id,
                        plan.bc_id,
                    )
        except AccountAdmissionDeferred as error:
            _defer_plan(
                plan, now + timedelta(milliseconds=max(1000, error.retry_after_ms))
            )
        except DomainError as error:
            if error.code in {
                "read_deadline_exceeded",
                "tiktok_call_deadline_exceeded",
            }:
                _defer_plan(plan, now + timedelta(seconds=5))
            elif (
                error.code == "report_permission_denied"
                or error.code == "tiktok_response_error"
                and any(v in error.message.lower() for v in ("permission", "权限"))
            ):
                plan.bc_report_supported = False
                _defer_plan(plan, now + timedelta(minutes=1))
            else:
                plan.disabled_reason = error.code
                _defer_plan(plan, now + timedelta(minutes=5))
        finally:
            if counted is not None:
                planner.record_calls(redis_client, now, counted.count)


def _dispatch_report_jobs(
    session: Session, now: datetime, *, allow_backfill=True
) -> int:
    # 多个调度唤醒也必须原子核对全局窗口，不能各自看到两个空槽后重复领取。
    locked = (
        session.connection()
        .execute(
            text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:report-dispatch-window', 0))"
            )
        )
        .scalar_one()
    )
    if not locked:
        return 0
    jobs = []
    for lane, window, task in (
        ("live", REPORT_DISPATCH_WINDOW, "reporting.job_step"),
        ("links", LINK_DISPATCH_WINDOW, "reporting.job_step"),
        ("backfill", BACKFILL_DISPATCH_WINDOW, "reporting.backfill_step"),
    ):
        if lane == "backfill" and not allow_backfill:
            continue
        inflight = session.exec(
            select(func.count())
            .select_from(ReportSyncJob)
            .where(
                ReportSyncJob.status == "RUNNING",
                col(ReportSyncJob.claimed_until) > now,
                lane_condition(lane=lane, now=now),
            )
        ).one()
        available = max(0, window - inflight)
        claimed = (
            claim_jobs(
                session,
                now=now,
                limit=available,
                lane=lane,
                allow_historical_links=allow_backfill,
            )
            if available
            else []
        )
        for job in claimed:
            enqueue_after_commit(
                session,
                context=_context(job),
                task_name=task,
                task_key=f"report-job:{job.id}:{job.claim_token}",
                payload={"job_id": str(job.id), "claim_id": str(job.claim_token)},
            )
        jobs.extend(claimed)
    for tenant_id in sorted({job.tenant_id for job in jobs}):
        # 游标与领取/outbox 同事务提交，失败不跳过租户，也不干扰其他业务队列。
        statement = insert(ReportDispatchCursor).values(
            tenant_id=tenant_id, last_dispatched_at=now
        )
        session.exec(
            statement.on_conflict_do_update(
                index_elements=["tenant_id"],
                set_={
                    "last_dispatched_at": func.greatest(
                        col(ReportDispatchCursor.last_dispatched_at),
                        statement.excluded.last_dispatched_at,
                    )
                },
            )
        )
    return len(jobs)


@contextmanager
def _report_refill_after(database_engine, redis_client, job):
    try:
        yield
    finally:
        if settings.ADS_SYNC_ENABLED:
            try:
                now = datetime.now(UTC)
                allow_backfill = (
                    planner.calls_today(redis_client, now)
                    <= settings.TIKTOK_REPORT_DAILY_CALL_BUDGET * 0.7
                )
                # 当前步骤提交后直接执行有锁本地领取；新消息仍写正常 outbox，
                # 去掉先发 control 唤醒再领取的双跳，不在读取 Worker 内发送 broker。
                # 仅补报表槽；目录保留自身完成唤醒和Control调度，不能每个事实步骤
                # 都重新搬运其独立积压、延长报表窗口锁和CPU占用。
                _dispatch_pending(
                    database_engine,
                    now,
                    allow_backfill=allow_backfill,
                    include_directories=False,
                )
            except Exception:
                # 已提交的报表不能因补位消息失败改成失败；30 秒 Beat 会再次补位。
                logger.exception("report direct refill failed: job=%s", job.id)


def _dispatch_pending(
    database_engine, now, *, allow_backfill=True, include_directories=True
):
    with Session(database_engine) as session, session.begin():
        return _dispatch_report_jobs(session, now, allow_backfill=allow_backfill) + (
            _dispatch_directories(session, now) if include_directories else 0
        )


def _failed(session, job, code):
    job.status, job.error_code, job.finished_at = "FAILED", code, datetime.now(UTC)
    job.claim_token = job.claimed_until = None
    publish.mark_failed(
        session,
        tenant_id=job.tenant_id,
        advertiser_ids=job.advertiser_ids,
        kind=job.kind,
        start=job.start_date,
        end=job.end_date,
        error_code=code,
        requested_at=request_time(job),
    )


def run_job_step(*, database_engine, redis_client, job_id, claim_token):
    with Session(database_engine, expire_on_commit=False) as session:
        job = session.get(ReportSyncJob, job_id, with_for_update=True)
        now = datetime.now(UTC)
        if job is None or job.claim_token != claim_token or job.status != "RUNNING":
            return "FAILED"
        # 旧积压消息不能越过有界派发窗口自行续租；只结束本条消息，
        # 业务运行仍留给扫描器按当前优先级重新领取，不记终态失败。
        if job.claimed_until is None or job.claimed_until <= now:
            return "FAILED"
        if not settings.ADS_SYNC_ENABLED:
            job.status, job.due_at = "WAITING", now + timedelta(minutes=1)
            job.claim_token = job.claimed_until = None
            session.commit()
            return "WAITING"
        try:
            route = FrozenTikTokRoute.model_validate(job.frozen_route)
            if route.channel != "OFFICIAL_API":
                raise DomainError("api_binding_required", "报表需要 API 绑定")
            for advertiser in job.advertiser_ids:
                verify_route(
                    session,
                    context=_context(job),
                    route=route,
                    advertiser_id=advertiser,
                    capability="read",
                )
        except DomainError as error:
            _failed(session, job, error.code)
            session.commit()
            return "FAILED"
        before = job.request_count
        # 排队等待不侵占实际读取期限，避免刚开始执行就被扫描器换代。
        job.claimed_until = now + LEASE
        session.commit()

    def factory():
        return Session(database_engine)

    with _report_refill_after(database_engine, redis_client, job):
        try:
            with _reader_for(
                database_engine=database_engine,
                redis_client=redis_client,
                plan=job,
                deadline=now + timedelta(seconds=170),
            ) as reader:
                if reader is None:
                    raise DomainError("api_binding_required", "报表需要 API 绑定")
                result = execute_job(
                    factory, job_id=job_id, claim_token=claim_token, reader=reader
                )
        except AccountAdmissionDeferred as error:
            with factory() as session:
                current = session.get(ReportSyncJob, job_id, with_for_update=True)
                if current is not None and current.claim_token == claim_token:
                    current.status = "WAITING"
                    current.error_code = "admission_deferred"
                    current.due_at = now + timedelta(
                        milliseconds=max(1000, error.retry_after_ms)
                    )
                    current.claim_token = current.claimed_until = None
                    session.commit()
            return "WAITING"
        except DomainError as error:
            with factory() as session:
                current = session.get(ReportSyncJob, job_id, with_for_update=True)
                if current is not None and current.claim_token == claim_token:
                    _failed(session, current, error.code)
                    session.commit()
            return "FAILED"
        with factory() as session:
            current = session.get(ReportSyncJob, job_id)
            if current is not None:
                # 计增量，不能在每次重试重复计入历史请求数。
                planner.record_calls(
                    redis_client, now, max(0, current.request_count - before)
                )
        return result


# 目录分页暂存只服务于本次运行的整批发布；运行发布或作废后不再被读取。
# 只保留 1 小时：每日新增约 1.8 万页，保留越久发布备份与恢复越慢。FAILED 可重试，不清理。
DIRECTORY_PAGE_RETENTION = timedelta(hours=1)
# 单批清理的运行数上限：暂存页含大 JSONB，分批各自提交，避免长事务。
DIRECTORY_PAGE_CLEANUP_RUNS = 2000
# 每轮任务内继续分批清理的时间预算（任务软超时 55 秒）；积压时下一轮继续。
DIRECTORY_PAGE_CLEANUP_SECONDS = 30


def _supersede_stalled_directory_runs(now):
    """同一账户、层级、广告类型已有更新运行发布后，较早的未完成运行不可能再发布
    （publish_directory 会以 directory_run_stale 拒绝）：标记为 STALE，暂存页随后清理。
    与 0031 迁移取消堆积运行同口径；租约有效的运行留给持有者结束，不抢改。"""
    newer = aliased(AdDirectoryRun)
    return (
        update(AdDirectoryRun)
        .where(
            col(AdDirectoryRun.status).in_(["QUEUED", "RUNNING"]),
            col(AdDirectoryRun.published_version).is_(None),
            col(AdDirectoryRun.claimed_until).is_(None)
            | (col(AdDirectoryRun.claimed_until) <= now),
            col(AdDirectoryRun.created_at) < now - DIRECTORY_PAGE_RETENTION,
            select(newer.id)
            .where(
                newer.tenant_id == AdDirectoryRun.tenant_id,
                newer.advertiser_id == AdDirectoryRun.advertiser_id,
                newer.kind == AdDirectoryRun.kind,
                newer.ad_type == AdDirectoryRun.ad_type,
                col(newer.request_sequence) > AdDirectoryRun.request_sequence,
                col(newer.published_version).is_not(None),
            )
            .exists(),
        )
        .values(
            status="STALE",
            error_code="superseded",
            completed_at=now,
            claim_token=None,
            claimed_until=None,
        )
    )


def _expired_directory_runs(now):
    finished = col(AdDirectoryRun.published_version).is_not(None) | col(
        AdDirectoryRun.status
    ).in_(["CANCELLED", "STALE"])
    return (
        select(AdDirectoryRun.id)
        .where(
            finished,
            # 被新运行取代的运行（superseded）不再需要排查窗口，立即清理。
            (col(AdDirectoryRun.error_code) == "superseded")
            | (
                func.coalesce(AdDirectoryRun.completed_at, AdDirectoryRun.created_at)
                < now - DIRECTORY_PAGE_RETENTION
            ),
            select(AdDirectoryPage.run_id)
            .where(AdDirectoryPage.run_id == AdDirectoryRun.id)
            .exists(),
        )
        .limit(DIRECTORY_PAGE_CLEANUP_RUNS)
    )


def cleanup_store(session, *, now):
    result = {}
    for key, model, condition in (
        (
            "jobs",
            ReportSyncJob,
            col(ReportSyncJob.finished_at) < now - timedelta(days=7),
        ),
        (
            "hours",
            CampaignReportHour,
            col(CampaignReportHour.stat_hour) < now - timedelta(days=30),
        ),
        ("snapshots", QuerySnapshot, col(QuerySnapshot.expires_at) < now),
        (
            "balances",
            ReportBalanceRun,
            (col(ReportBalanceRun.created_at) < now - timedelta(days=7))
            & (col(ReportBalanceRun.status).in_(["COMPLETE", "FAILED"])),
        ),
    ):
        result[key] = (
            session.connection().execute(delete(model).where(condition)).rowcount
        )
    result["directory_superseded"] = (
        session.connection().execute(_supersede_stalled_directory_runs(now)).rowcount
    )
    result["directory_pages"] = cleanup_directory_pages(session, now=now)
    result["material_pages"] = cleanup_material_pages(session, now=now)
    return result


def cleanup_directory_pages(session, *, now):
    """删除一批已发布或已作废运行的目录暂存页；返回删除的页数。"""
    return (
        session.connection()
        .execute(
            delete(AdDirectoryPage).where(
                col(AdDirectoryPage.run_id).in_(_expired_directory_runs(now))
            )
        )
        .rowcount
    )


@celery_app.task(name="reporting.plan_due", time_limit=30, soft_time_limit=25)
def plan_due(*, tenant_id=None, actor_id=None, payload=None):
    del tenant_id, actor_id
    if payload == {"dispatch_only": True}:
        if not settings.ADS_SYNC_ENABLED:
            return 0
        now = datetime.now(UTC)
        with Redis.from_url(settings.REDIS_URL) as redis:
            allow_backfill = (
                planner.calls_today(redis, now)
                <= settings.TIKTOK_REPORT_DAILY_CALL_BUDGET * 0.7
            )
        # 报表与目录共用轻量补位信号；只领取本地持久任务，不重做外部筛选。
        return _dispatch_pending(engine, now, allow_backfill=allow_backfill)
    with Redis.from_url(settings.REDIS_URL) as redis:
        return plan_due_runs(database_engine=engine, redis_client=redis)


@celery_app.task(
    name="reporting.job_step",
    time_limit=200,
    soft_time_limit=180,
    acks_late=True,
    reject_on_worker_lost=True,
)
def job_step(*, tenant_id, actor_id, payload):
    validate_dispatch_payload(payload)
    if set(payload) != {"job_id", "claim_id"}:
        raise DomainError("dispatch_payload_invalid", "报表任务参数无效")
    job_id, token = UUID(payload["job_id"]), UUID(payload["claim_id"])
    with Session(engine) as session:
        job = session.get(ReportSyncJob, job_id)
        if (
            job is None
            or str(job.tenant_id) != tenant_id
            or str(job.actor_id) != actor_id
        ):
            raise DomainError("action_forbidden", "报表任务操作者不匹配")
    with Redis.from_url(settings.REDIS_URL) as redis:
        return run_job_step(
            database_engine=engine, redis_client=redis, job_id=job_id, claim_token=token
        )


@celery_app.task(
    name="reporting.backfill_step",
    time_limit=200,
    soft_time_limit=180,
    acks_late=True,
    reject_on_worker_lost=True,
)
def backfill_step(*, tenant_id, actor_id, payload):
    # 相同执行合同与冻结权限，只把慢任务的物理执行容量隔离。
    return job_step.run(tenant_id=tenant_id, actor_id=actor_id, payload=payload)


@celery_app.task(name="reporting.cleanup_store", time_limit=60, soft_time_limit=55)
def cleanup_store_task():
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        result = cleanup_store(session, now=now)
    # 首次上线时暂存页积压数 GB：在时间预算内继续分批，每批独立提交。
    deadline = time.monotonic() + DIRECTORY_PAGE_CLEANUP_SECONDS
    removed = result["directory_pages"]
    while removed and time.monotonic() < deadline:
        with Session(engine) as session, session.begin():
            removed = cleanup_directory_pages(session, now=now)
        result["directory_pages"] += removed
    return result
