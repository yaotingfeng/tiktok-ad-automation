"""报表后台入口：冻结授权、分档规划、持久派发与有限租约恢复。"""

import json
import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

from billiard.exceptions import SoftTimeLimitExceeded
from redis import Redis
from sqlalchemy import case, delete, func, text, tuple_, update
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
    PUBLISH_TASK_NAME,
    TARGETED_TASK_NAME,
    directory_step_key,
    enqueue_directory_step,
    is_live_directory,
    targeted_capacity,
)
from app.modules.ads.directory_requests import (
    directory_ad_types,
    directory_due_accounts,
    request_directory,
)
from app.modules.ads.material_directory_children import MATERIAL_CHILD_KEY
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting import planner, publish
from app.modules.reporting.jobs import (
    LEASE,
    _CountingReader,
    _read_all,
    claim_jobs,
    execute_job,
    request_time,
)
from app.modules.reporting.lanes import (
    lane_condition,
    report_workload,
)
from app.modules.reporting.metrics import parse_decimal
from app.modules.reporting.partitions import ensure_month_partitions
from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget
from app.modules.reporting.query_models import QuerySnapshot, QuerySnapshotRow
from app.modules.reporting.store_models import (
    AdSyncBc,
    CampaignReportHour,
    DirectoryDispatchCursor,
    ReportBalanceRun,
    ReportDispatchCursor,
    ReportSyncJob,
    ReportSyncPage,
)

logger = logging.getLogger(__name__)

# 每次派发最多入队的目录运行数；其余留给后续轮次，保证规划耗时有界。
DIRECTORY_DISPATCH_BATCH = 1000
# 历史定向与全量目录共用后台窗口；只在独立后台消费者执行，不占实时目录槽。
DIRECTORY_DISPATCH_WINDOW = 8
# 事实与关联独立限制在途任务；这是领取上限，不代表共享 Worker 中的专属执行槽。
# 12 个执行进程保留两批有界候补，吸收 outbox 投递空档；
# 实际 SDK 并发和调用次数始终由共享准入额度约束。
REPORT_DISPATCH_WINDOW = 24
# 昨日收尾只保留两个独立实时候补；今日先派发且原 24 个名额不被收尾占用。
# 这是数据库在途限制，实际执行仍共享 reporting Worker 与平台准入额度。
CLOSEOUT_DISPATCH_WINDOW = 2
# 当地今天关联独立于实时事实；历史关联使用下面的后台窗口。
LINK_DISPATCH_WINDOW = 4
# 后台3个执行进程同样保留两批有界候补，避免每步等待outbox投递而空转。
# 历史事实/关联各保底一半，闲置份额可借；实际执行并发及平台配额不变。
BACKFILL_DISPATCH_WINDOW = 6
# 目录消息发布后超过此时长仍未被领取，视为丢失并重新入队。
# 读取孤儿10分钟恢复；单槽发布可执行至210秒，8个窗口最坏排队低于30分钟。
# 发布单独判丢失，避免长队中合法消息被反复换代；行锁/代数仍防重复发布。
# 实时定向仍使用更短的2分钟。
DIRECTORY_MESSAGE_LOST_AFTER = timedelta(minutes=10)
PUBLISH_MESSAGE_LOST_AFTER = timedelta(minutes=30)
TARGETED_MESSAGE_LOST_AFTER = timedelta(minutes=2)
# 一轮周期目录分批推进；此游标不占用账户筛选或规则相位的进度键。
DIRECTORY_PLAN_PROGRESS = "directory_cycle_v1"
DIRECTORY_PLAN_ACCOUNTS = 64
DIRECTORY_PLAN_SECONDS = 3

register_dispatch_task("reporting.job_step", "ads-reporting")
register_dispatch_task("reporting.backfill_step", "ads-backfill")
register_dispatch_task("reporting.plan_due", "control")


def _context(row):
    return TenantContext(
        tenant_id=row.tenant_id, actor_id=row.actor_id, role="operator"
    )


@contextmanager
def _reader_for(*, database_engine, redis_client, plan, deadline, workload="live"):
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=_context(plan),
        route=FrozenTikTokRoute.model_validate(plan.frozen_route),
        task_deadline=deadline,
        workload=workload,
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
    dispatch = session.exec(
        select(PendingDispatch.published_at, PendingDispatch.task_name).where(
            PendingDispatch.tenant_id == row.tenant_id,
            PendingDispatch.task_key == key,
        )
    ).first()
    if dispatch is None or dispatch.published_at is None:
        return False
    threshold = (
        PUBLISH_MESSAGE_LOST_AFTER
        if dispatch.task_name == PUBLISH_TASK_NAME
        else TARGETED_MESSAGE_LOST_AFTER
        if is_live_directory(row)
        else DIRECTORY_MESSAGE_LOST_AFTER
    )
    return dispatch.published_at < now - threshold


def _dispatch_directories(session, now):
    # 本代数、本页的投递记录已存在（无论是否已发布）就不再重复入队：积压时
    # 若每轮重写全部运行，规划会长时间占住 control 唯一并发，使投递发布被饿死。
    targeted = (col(AdDirectoryRun.workload) == "live") & (
        (func.coalesce(func.jsonb_array_length(AdDirectoryRun.query["ids"]), 0) > 0)
        | (
            func.coalesce(
                func.jsonb_array_length(AdDirectoryRun.query["parent_ids"]), 0
            )
            > 0
        )
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
                (
                    col(PendingDispatch.task_name) == PUBLISH_TASK_NAME,
                    now - PUBLISH_MESSAGE_LOST_AFTER,
                ),
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
            # 消息判丢失前窗口被永久占满。与下方候选条件一致，过期运行会换代重新入队。
            (enqueued & ~lost & col(AdDirectoryRun.claimed_until).is_(None))
            | (col(AdDirectoryRun.claimed_until) > now),
        )
    ).one()
    available = max(0, DIRECTORY_DISPATCH_WINDOW - inflight)
    targeted_available = targeted_capacity(session, now)
    # 旧全账户创意扫描仍降后；主目录派生的素材子页沿原请求年龄续跑，
    # 否则会永远排在对象队尾，并在执行前被下一轮主观察淘汰。
    legacy_creative = (col(AdDirectoryRun.kind) == "creative") & ~col(
        AdDirectoryRun.query
    ).op("?")(MATERIAL_CHILD_KEY)
    creative_last = case((legacy_creative, 1), else_=0)
    newer_scan = aliased(AdDirectoryRun)
    # 历史素材全量扫描曾重复堆积。同一冻结授权和完整查询仅先推进最新一轮，
    # 它成功发布后再由原 superseded 机制淘汰旧轮；不合并分页、不把旧轮记为成功。
    newer_creative_pending = (
        select(newer_scan.id)
        .where(
            newer_scan.tenant_id == AdDirectoryRun.tenant_id,
            newer_scan.advertiser_id == AdDirectoryRun.advertiser_id,
            newer_scan.bc_id == AdDirectoryRun.bc_id,
            newer_scan.actor_id == AdDirectoryRun.actor_id,
            newer_scan.frozen_route == AdDirectoryRun.frozen_route,
            newer_scan.partition_key == AdDirectoryRun.partition_key,
            col(newer_scan.request_sequence) > col(AdDirectoryRun.request_sequence),
            col(newer_scan.status).in_(["QUEUED", "RUNNING", "WAITING_REMOTE"]),
        )
        .exists()
    )
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
            ~(~targeted & legacy_creative & newer_creative_pending),
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
        if is_live_directory(row):
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
    allow_backfill = planner.backfill_allowed(redis_client, now)
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    # 额度紧张优先让出历史补数，不静默把止损数据降成小时级。
    hot, material = planner.HOT_INTERVAL, planner.MATERIAL_INTERVAL
    with Session(database_engine) as session, session.begin():
        ensure_month_partitions(session, today=now.date())
        planner.ensure_bc_plans(session, now=now)
        keys = [(p.tenant_id, p.bc_id) for p in planner.due_plans(session, now=now)]
    dispatched = _dispatch_pending(database_engine, now, allow_backfill=allow_backfill)
    # 本地归并/依赖复核不能阻挡事实派发；独立短事务、总预算与单语句超时双重有界。
    try:
        from app.modules.reporting.link_work import (
            reconcile_link_work,
            wake_link_dependencies,
        )

        with Session(database_engine) as session, session.begin():
            session.connection().execute(text("SET LOCAL statement_timeout='2s'"))
            session.connection().execute(text("SET LOCAL lock_timeout='100ms'"))
            maintenance_deadline = datetime.now(UTC) + timedelta(seconds=2)
            reconcile_link_work(session, now=now, deadline=maintenance_deadline)
            if datetime.now(UTC) < maintenance_deadline:
                wake_link_dependencies(session, now=now, deadline=maintenance_deadline)
    except Exception:
        logger.exception("report link maintenance deferred")
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
        retry_at = now + timedelta(minutes=5)
        _defer_plan(plan, retry_at)
        plan.next_material_at = max(plan.next_material_at, retry_at)


def _defer_plan(plan, retry_at):
    # 账户筛选、历史和目录一起退避；素材使用已知活跃账户独立投递，
    # 不能因上游筛选退避而整轮漏投。
    for field in (
        "next_hot_at",
        "next_daily_at",
        "next_directory_at",
    ):
        setattr(plan, field, max(getattr(plan, field), retry_at))


def _plan_directory_step(session, plan, *, accounts, now):
    """稳定账户ID游标；成功批次才推进，单个慢账户退避后不阻挡其余账户。"""
    accounts = sorted(set(accounts))
    scope = sha256(
        json.dumps(
            [
                str(plan.tenant_id),
                plan.bc_id,
                str(plan.actor_id),
                plan.frozen_route,
                accounts,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    state: dict[str, Any] = dict(
        plan.screening_progress.get(DIRECTORY_PLAN_PROGRESS) or {}
    )
    if state.get("scope") != scope:
        state = {
            "scope": scope,
            "anchor": plan.next_directory_at.isoformat(),
            "after": "",
            "batch_size": DIRECTORY_PLAN_ACCOUNTS,
            "deferred": {},
        }
    anchor = datetime.fromisoformat(state["anchor"])
    owned = set(accounts)
    deferred = {a: at for a, at in state.get("deferred", {}).items() if a in owned}
    retry = sorted(a for a, at in deferred.items() if datetime.fromisoformat(at) <= now)
    fresh = [a for a in accounts if a > state["after"] and a not in deferred]
    size = max(1, min(DIRECTORY_PLAN_ACCOUNTS, int(state["batch_size"])))
    batch = (retry + fresh)[:size]
    if batch:
        try:
            with (
                session.begin_nested(),
                query_budget(session, seconds=DIRECTORY_PLAN_SECONDS),
            ):
                due = directory_due_accounts(
                    session,
                    tenant_id=plan.tenant_id,
                    bc_id=plan.bc_id,
                    actor_id=plan.actor_id,
                    frozen_route=plan.frozen_route,
                    advertiser_ids=batch,
                    active_ids=plan.last_active_advertiser_ids,
                    now=now,
                )
                request_directory(
                    session,
                    tenant_id=plan.tenant_id,
                    bc_id=plan.bc_id,
                    actor_id=plan.actor_id,
                    frozen_route=plan.frozen_route,
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
                session.flush()
        except (DBAPIError, QueryBudgetExceeded) as error:
            if (
                isinstance(error, DBAPIError)
                and getattr(error.orig, "sqlstate", None) != "57014"
            ):
                raise
            state["batch_size"] = max(1, size // 2)
            if len(batch) == 1:
                # 失败账户仍持久保留为未完成；只让其他账户继续，不将其伪记为完成。
                deferred[batch[0]] = (now + timedelta(minutes=5)).isoformat()
            logger.warning(
                "directory planning batch deferred: tenant=%s bc=%s accounts=%s",
                plan.tenant_id,
                plan.bc_id,
                len(batch),
            )
        else:
            advanced = [a for a in batch if a not in deferred]
            if advanced:
                state["after"] = max(state["after"], *advanced)
            for advertiser in batch:
                deferred.pop(advertiser, None)
            state["batch_size"] = min(DIRECTORY_PLAN_ACCOUNTS, size * 2)
    state["deferred"] = deferred
    remaining = [a for a in accounts if a > state["after"] and a not in deferred]
    if not remaining and not deferred:
        plan.screening_progress = {
            k: v
            for k, v in plan.screening_progress.items()
            if k != DIRECTORY_PLAN_PROGRESS
        }
        plan.next_directory_at = _advance(anchor, now, planner.DIRECTORY_INTERVAL)
    else:
        plan.screening_progress = {
            **plan.screening_progress,
            DIRECTORY_PLAN_PROGRESS: state,
        }
        plan.next_directory_at = (
            now + timedelta(seconds=5)
            if remaining
            else min(datetime.fromisoformat(at) for at in deferred.values())
        )


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
    assert hot == material, "双实时事实必须使用相同周期"
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
        # 到期状态必须在外部筛选和退避前冻结；素材时钟单独到期不代表热报漏投。
        hot_due_at = plan.next_hot_at if plan.next_hot_at <= now else None
        material_due_at = (
            plan.next_material_at if plan.next_material_at <= now else None
        )
        counted = None
        hot_planned = False
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
                        hot_planned = True
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
                        _plan_directory_step(session, plan, accounts=accounts, now=now)
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
        # 已知活跃账户的两类实时事实共用 15 分钟周期；筛选退避时仍刷新
        # 当地今日已有事实，规则不额外扩展到无消耗账户，避免为无数据判断空转。
        if plan.enabled and (hot_due_at is not None or material_due_at is not None):
            refresh_accounts = set(
                plan.last_active_advertiser_ids
            ) | planner.known_reporting_accounts(
                session, plan, accounts=accounts, now=now
            )
            if hot_due_at is not None and not hot_planned:
                planner.plan_hot(
                    session, plan, active=sorted(refresh_accounts), now=now
                )
            planner.plan_material(
                session, plan, active=sorted(refresh_accounts), now=now
            )
            if hot_due_at is not None:
                # 切日后今天可能尚无消耗；昨日收尾按 owned 昨日事实独立选账户。
                planner.plan_day_closeout(session, plan, now=now)
            # 热报是双时钟相位基准：错相旧计划在下一轮自动归一；筛选失败也已
            # 为已知账户补投本轮事实，不再把退避时间当成新的热报相位。
            next_live = (
                _advance(hot_due_at, now, hot)
                if hot_due_at is not None
                else plan.next_hot_at
            )
            plan.next_hot_at = plan.next_material_at = next_live


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
        ("closeout", CLOSEOUT_DISPATCH_WINDOW, "reporting.job_step"),
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
        claimed = []
        if available and lane == "backfill":
            # 历史事实与关联在补数池内各留份额，再借用空闲槽；不能让补数池空闲，
            # 却把历史关联排到实时报表后面。窗口/在途计数仍在同一调度锁内。
            for group in ("facts", "links"):
                kind_filter = (
                    ReportSyncJob.kind == "material_links"
                    if group == "links"
                    else ReportSyncJob.kind != "material_links"
                )
                running = session.exec(
                    select(func.count())
                    .select_from(ReportSyncJob)
                    .where(
                        kind_filter,
                        ReportSyncJob.status == "RUNNING",
                        col(ReportSyncJob.claimed_until) > now,
                        lane_condition(lane=lane, now=now),
                    )
                ).one()
                share = (window + (group == "facts")) // 2
                count = min(available - len(claimed), max(0, share - running))
                if count:
                    batch = claim_jobs(
                        session, now=now, limit=count, lane=lane, backfill_group=group
                    )
                    claimed.extend(batch)
                    _record_report_dispatch(session, batch, now)
        remaining = available - len(claimed)
        if remaining:
            claimed.extend(
                claim_jobs(
                    session,
                    now=now,
                    limit=remaining,
                    lane=lane,
                )
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
    _record_report_dispatch(session, jobs, now)
    return len(jobs)


def _record_report_dispatch(session, jobs, now):
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


@contextmanager
def _report_refill_after(database_engine, redis_client, job):
    try:
        yield
    finally:
        if settings.ADS_SYNC_ENABLED:
            try:
                now = datetime.now(UTC)
                allow_backfill = planner.backfill_allowed(redis_client, now)
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
        workload = report_workload(session, job, now=now)
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
                workload=workload,
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
# 每分钟以10秒为续批预算，首轮每类仍有一次机会；短批各自提交，持续追赶暂存。
DIRECTORY_PAGE_CLEANUP_SECONDS = 10
CLEANUP_CURSOR_KEY = "reporting:cleanup:cursors:v2"
# 先按主键取有限候选，再判断保留期，避免无时间索引时 LIMIT 仍扫描全表。
CLEANUP_SCAN_ROWS = 1000
CLEANUP_BATCH_ROWS = 100
# 无页父任务很小，单批放大到1000；大JSONB子页仍独立限制100页。
CLEANUP_JOB_BATCH_ROWS = 1000
CLEANUP_STATEMENT_MS = 2000
CLEANUP_LOCK_MS = 200
CLEANUP_CATEGORIES = (
    "material_pages",
    "jobs",
    "hours",
    "snapshots",
    "balances",
    "directory_superseded",
    "directory_pages",
)


def _supersede_stalled_directory_runs(now, *, candidate_ids=None, batch_limit=None):
    """同一账户、层级、广告类型已有更新运行发布后，较早的未完成运行不可能再发布
    （publish_directory 会以 directory_run_stale 拒绝）：标记为 STALE，暂存页随后清理。
    与 0031 迁移取消堆积运行同口径；租约有效的运行留给持有者结束，不抢改。"""
    newer = aliased(AdDirectoryRun)
    candidates = select(AdDirectoryRun.id).where(
        col(AdDirectoryRun.status).in_(["QUEUED", "RUNNING", "WAITING_REMOTE"]),
        col(AdDirectoryRun.published_version).is_(None),
        col(AdDirectoryRun.claimed_until).is_(None)
        | (col(AdDirectoryRun.claimed_until) <= now),
        col(AdDirectoryRun.created_at) < now - DIRECTORY_PAGE_RETENTION,
        # 素材子运行覆盖冻结父组，不能以同账户其他组的发布淘汰。
        ~col(AdDirectoryRun.query).op("?")(MATERIAL_CHILD_KEY),
        select(newer.id)
        .where(
            newer.tenant_id == AdDirectoryRun.tenant_id,
            newer.advertiser_id == AdDirectoryRun.advertiser_id,
            newer.kind == AdDirectoryRun.kind,
            newer.ad_type == AdDirectoryRun.ad_type,
            col(newer.request_sequence) > AdDirectoryRun.request_sequence,
            col(newer.published_version).is_not(None),
            ~col(newer.query).op("?")(MATERIAL_CHILD_KEY),
        )
        .exists(),
    )
    if candidate_ids is not None:
        candidates = candidates.where(col(AdDirectoryRun.id).in_(candidate_ids))
    return (
        update(AdDirectoryRun)
        .where(
            col(AdDirectoryRun.id).in_(
                candidates.limit(batch_limit or CLEANUP_BATCH_ROWS).with_for_update(
                    skip_locked=True
                )
            )
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


def _cleanup_primary_key(model):
    return tuple(model.__table__.primary_key.columns)


def _cleanup_candidates(session, model, *, cursor, limit):
    """主键窗口本身不附带过期条件；读取上限不受历史表大小或命中率影响。"""
    keys = _cleanup_primary_key(model)
    statement = select(*keys).order_by(*keys).limit(limit)
    if cursor:
        values = []
        for column, value in zip(keys, cursor, strict=True):
            try:
                kind = column.type.python_type
            except NotImplementedError:
                # SQLModel AutoString 不实现 python_type，实际存储类型由 impl 给出。
                # UUID 和带时区日期仍按原类型恢复，不能把整个复合游标当字符串。
                kind = column.type.impl.python_type
            values.append(
                datetime.fromisoformat(value) if kind is datetime else kind(value)
            )
        statement = statement.where(tuple_(*keys) > tuple_(*values))
    return keys, list(session.connection().execute(statement).all())


def _cleanup_category(session, *, category, now, cursors):
    terminal = col(ReportSyncJob.status).in_(["COMPLETE", "FAILED", "CANCELLED"])
    expired_jobs = terminal & (col(ReportSyncJob.finished_at) < now - timedelta(days=7))
    models = {
        "jobs": (ReportSyncJob, expired_jobs),
        "material_pages": (ReportSyncJob, expired_jobs),
        "hours": (
            CampaignReportHour,
            col(CampaignReportHour.stat_hour) < now - timedelta(days=30),
        ),
        "snapshots": (QuerySnapshot, col(QuerySnapshot.expires_at) < now),
        "balances": (
            ReportBalanceRun,
            (col(ReportBalanceRun.created_at) < now - timedelta(days=7))
            & col(ReportBalanceRun.status).in_(["COMPLETE", "FAILED"]),
        ),
        "directory_superseded": (AdDirectoryRun, None),
        "directory_pages": (AdDirectoryRun, None),
    }
    model, condition = models[category]
    limits = cursors.get("_limits", {}).get(category, {})
    batch_limit = limits.get(
        "batch", CLEANUP_JOB_BATCH_ROWS if category == "jobs" else CLEANUP_BATCH_ROWS
    )
    # 分页从子表自己的主键窗口出发，避免为找到少量有页任务反复读无页父记录。
    # v2游标记录(job_id/run_id,page)，旧v1父表游标不复用；重扫不会越过保留策略。
    page_model = {
        "material_pages": ReportSyncPage,
        "directory_pages": AdDirectoryPage,
    }.get(category)
    keys, candidates = _cleanup_candidates(
        session,
        page_model or model,
        cursor=cursors.get(category),
        limit=limits.get("scan", CLEANUP_SCAN_ROWS),
    )
    if not candidates:
        cursors[category] = None
        cursors["_exhausted"] = True
        return 0
    parent_keys = _cleanup_primary_key(model)
    scope = (
        parent_keys[0].in_(list(dict.fromkeys(row[0] for row in candidates)))
        if page_model is not None
        else tuple_(*keys).in_(candidates)
    )
    eligible = select(*parent_keys).where(scope)
    if condition is not None:
        eligible = eligible.where(condition)
    has_pages = (
        select(ReportSyncPage.job_id)
        .where(ReportSyncPage.job_id == ReportSyncJob.id)
        .exists()
    )
    if category == "material_pages":
        eligible = eligible.where(has_pages)
    elif category == "jobs":
        eligible = eligible.where(~has_pages)
    eligible = eligible.limit(batch_limit).with_for_update(skip_locked=True)
    child_removed = 0
    if category == "material_pages":
        page_keys = (
            select(ReportSyncPage.job_id, ReportSyncPage.page)
            .where(col(ReportSyncPage.job_id).in_(eligible))
            .limit(batch_limit)
            .with_for_update(skip_locked=True)
        )
        statement = delete(ReportSyncPage).where(
            tuple_(col(ReportSyncPage.job_id), col(ReportSyncPage.page)).in_(page_keys)
        )
    elif category == "jobs":
        # 大 JSONB 子页另按页清理；只删已无页的父任务，禁止隐式大级联。
        statement = delete(ReportSyncJob).where(
            expired_jobs,
            col(ReportSyncJob.id).in_(eligible),
            ~select(ReportSyncPage.job_id)
            .where(ReportSyncPage.job_id == ReportSyncJob.id)
            .exists(),
        )
    elif category == "directory_superseded":
        statement = _supersede_stalled_directory_runs(
            now, candidate_ids=[row[0] for row in candidates], batch_limit=batch_limit
        )
    elif category == "directory_pages":
        removed = cleanup_directory_pages(
            session,
            now=now,
            candidate_ids=[row[0] for row in candidates],
            batch_limit=batch_limit,
        )
        if not removed:
            cursors[category] = [str(value) for value in candidates[-1]]
        return removed
    else:
        if category == "snapshots":
            # 快照同样可能包含大量行；先排空有界子行，再删除父记录。
            rows = (
                select(QuerySnapshotRow.snapshot_id, QuerySnapshotRow.row_key)
                .where(col(QuerySnapshotRow.snapshot_id).in_(eligible))
                .limit(batch_limit)
                .with_for_update(skip_locked=True)
            )
            child_removed = (
                session.connection()
                .execute(
                    delete(QuerySnapshotRow).where(
                        tuple_(
                            col(QuerySnapshotRow.snapshot_id),
                            col(QuerySnapshotRow.row_key),
                        ).in_(rows)
                    )
                )
                .rowcount
            )
            eligible = eligible.where(
                ~select(QuerySnapshotRow.snapshot_id)
                .where(QuerySnapshotRow.snapshot_id == QuerySnapshot.id)
                .exists()
            )
        statement = delete(model).where(tuple_(*keys).in_(eligible))
    removed = session.connection().execute(statement).rowcount
    # 仍有删除进展就复查同一窗口，以便一个大任务逐页排空；空窗口才前进。
    if child_removed:
        cursors["_child_progress"] = True
    if not removed and not child_removed:
        cursors[category] = [str(value) for value in candidates[-1]]
    return removed


def cleanup_store(session, *, now, categories=CLEANUP_CATEGORIES, cursors=None):
    """每类只执行一批；调用方负责提交，不把某类超时扩散到其余保留策略。"""
    cursors = {} if cursors is None else cursors
    result = dict.fromkeys(categories, 0)
    for category in categories:
        try:
            with session.begin_nested():
                session.connection().execute(
                    text(f"SET LOCAL statement_timeout = '{CLEANUP_STATEMENT_MS}ms'")
                )
                session.connection().execute(
                    text(f"SET LOCAL lock_timeout = '{CLEANUP_LOCK_MS}ms'")
                )
                result[category] = _cleanup_category(
                    session, category=category, now=now, cursors=cursors
                )
            # 短暂拥塞恢复后逐级放大，避免一次超时永久降低清理吞吐。
            limits = cursors.get("_limits", {}).get(category)
            if limits:
                limits["scan"] = min(CLEANUP_SCAN_ROWS, limits["scan"] * 2)
                maximum = (
                    CLEANUP_JOB_BATCH_ROWS if category == "jobs" else CLEANUP_BATCH_ROWS
                )
                limits["batch"] = min(maximum, limits["batch"] * 2)
        except DBAPIError as error:
            # 数据库主动取消语句，savepoint 回滚后仍可处理其他类别；未知错误保留告警。
            if getattr(error.orig, "sqlstate", None) not in {"57014", "55P03"}:
                raise
            # 大窗口超时后下轮缩小重试，保持原游标；不能永久重跑同一超大批。
            limits = cursors.setdefault("_limits", {}).setdefault(category, {})
            limits["scan"] = max(1, limits.get("scan", CLEANUP_SCAN_ROWS) // 4)
            default_batch = (
                CLEANUP_JOB_BATCH_ROWS if category == "jobs" else CLEANUP_BATCH_ROWS
            )
            limits["batch"] = max(1, limits.get("batch", default_batch) // 4)
            cursors["_deferred"] = True
            if limits["scan"] == limits["batch"] == 1:
                logger.error(
                    "report cleanup requires single-row retry: category=%s cursor=%s",
                    category,
                    cursors.get(category),
                )
            logger.warning(
                "report cleanup batch deferred: category=%s sqlstate=%s",
                category,
                getattr(error.orig, "sqlstate", None),
            )
    return result


def cleanup_directory_pages(session, *, now, candidate_ids=None, batch_limit=None):
    """同时限制运行数与页数，单个大运行也不能产生无界 JSONB 删除。"""
    runs = _expired_directory_runs(now)
    if candidate_ids is not None:
        runs = runs.where(col(AdDirectoryRun.id).in_(candidate_ids))
    pages = (
        select(AdDirectoryPage.run_id, AdDirectoryPage.page)
        .where(col(AdDirectoryPage.run_id).in_(runs))
        .limit(batch_limit or CLEANUP_BATCH_ROWS)
        .with_for_update(skip_locked=True)
    )
    return (
        session.connection()
        .execute(
            delete(AdDirectoryPage).where(
                tuple_(col(AdDirectoryPage.run_id), col(AdDirectoryPage.page)).in_(
                    pages
                )
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
            allow_backfill = planner.backfill_allowed(redis, now)
        # 报表与目录共用轻量补位信号；只领取本地持久任务，不重做外部筛选。
        count = _dispatch_pending(engine, now, allow_backfill=allow_backfill)
        if count:
            from app.jobs.tasks import drain_dispatch

            # 领取事务已提交，再复用原一秒公平发送预算；新业务工作无需等第二个 tick。
            # 空补位不扫描 outbox，失败消息仍由原持久记录与 broker 退避恢复。
            drain_dispatch()
        return count
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
    # 预算从第一次数据库操作前计算，每类轮流一批；任何批失败都不撤销已提交进展。
    deadline = time.monotonic() + DIRECTORY_PAGE_CLEANUP_SECONDS
    result = dict.fromkeys(CLEANUP_CATEGORIES, 0)
    cursor_key = CLEANUP_CURSOR_KEY
    with Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
    ) as redis:
        # 并发 Beat/人工重跑不交错推进游标；锁比60秒任务硬上限长，崩溃后自动释放。
        lock = redis.lock("reporting:cleanup:lock:v1", timeout=120, blocking=False)
        if not lock.acquire(blocking=False):
            return result
        try:
            return _cleanup_store_batches(
                redis, now=now, deadline=deadline, result=result, cursor_key=cursor_key
            )
        finally:
            lock.release()


def _cleanup_store_batches(redis, *, now, deadline, result, cursor_key):
    cursors = {
        key: json.loads(value)
        for key, value in redis.hgetall(cursor_key).items()
        if key in CLEANUP_CATEGORIES or key == "_limits"
    }
    completed = set()
    retries = dict.fromkeys(CLEANUP_CATEGORIES, 0)
    first_round = True
    while first_round or time.monotonic() < deadline:
        progressed = False
        for category in CLEANUP_CATEGORIES:
            if category in completed:
                continue
            # 首轮给每类一次独立机会，避免前几类耗尽软预算后固定饿死尾部类别。
            if not first_round and time.monotonic() >= deadline:
                return result
            previous = cursors.get(category)
            for marker in ("_child_progress", "_exhausted", "_deferred"):
                cursors.pop(marker, None)
            with Session(engine) as session, session.begin():
                batch = cleanup_store(
                    session, now=now, categories=(category,), cursors=cursors
                )
            # 只有数据库提交成功才推进恢复位置；Redis丢失只会安全重扫。
            with redis.pipeline(transaction=True) as pipe:
                pipe.hset(cursor_key, category, json.dumps(cursors.get(category)))
                pipe.hset(cursor_key, "_limits", json.dumps(cursors.get("_limits", {})))
                pipe.expire(cursor_key, 7 * 24 * 3600)
                pipe.execute()
            result[category] += batch[category]
            if cursors.pop("_exhausted", False):
                completed.add(category)
            if cursors.pop("_deferred", False):
                retries[category] += 1
                # 先轮转给其他类别，再在预算内缩批重试；不必等下一小时才试小批。
                if retries[category] >= 5:
                    completed.add(category)
                else:
                    progressed = True
            progressed |= (
                bool(batch[category])
                or bool(cursors.pop("_child_progress", False))
                or cursors.get(category) != previous
            )
        first_round = False
        if not progressed:
            break
    return result
