"""余额按 BC 分页独立派发；每页提交后续接，不阻断效果报表。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis import Redis
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.celery_app import celery_app
from app.jobs.outbox import enqueue_after_commit, validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.ads.balances import persist_balance
from app.modules.reporting.planner import owned_accounts
from app.modules.reporting.store_models import AdSyncBc, ReportBalanceRun

TASK_NAME = "reporting.balance_step"
register_dispatch_task(TASK_NAME, "ads-directory")


def _enqueue_page(session, run):
    enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"{run.run_key}:page:{run.next_page}",
        payload={"run_id": str(run.id), "page": run.next_page},
    )


def enqueue_balances(session, plan, advertisers, now):
    if not settings.ADS_SYNC_ENABLED or not advertisers:
        return 0
    route_key = hashlib.sha256(
        json.dumps([plan.frozen_route, str(plan.actor_id)], sort_keys=True).encode()
    ).hexdigest()
    key = f"balance:{route_key}:{int(now.timestamp()) // 1800}"
    if session.exec(
        select(ReportBalanceRun.id).where(
            ReportBalanceRun.tenant_id == plan.tenant_id,
            ReportBalanceRun.run_key == key,
        )
    ).first():
        return 0
    run = ReportBalanceRun(
        tenant_id=plan.tenant_id,
        actor_id=plan.actor_id,
        run_key=key,
        frozen_route=plan.frozen_route,
        remaining_advertiser_ids=sorted(set(advertisers)),
        created_at=now,
    )
    session.add(run)
    session.flush()
    _enqueue_page(session, run)
    return 1


def plan_balances(*, database_engine, now=None):
    if not settings.ADS_SYNC_ENABLED:
        return 0
    now = now or datetime.now(UTC)
    # 与报表筛选及其退避完全分离，低预算时仍保持独立的半小时计划。
    with Session(database_engine) as session:
        keys = session.exec(
            select(AdSyncBc.tenant_id, AdSyncBc.bc_id).where(
                col(AdSyncBc.enabled).is_(True)
            )
        ).all()
    count = 0
    for tenant_id, bc_id in keys:
        with Session(database_engine) as session, session.begin():
            plan = session.exec(
                select(AdSyncBc)
                .where(AdSyncBc.tenant_id == tenant_id, AdSyncBc.bc_id == bc_id)
                .with_for_update(skip_locked=True)
            ).one_or_none()
            if plan is not None and plan.enabled:
                count += enqueue_balances(
                    session, plan, owned_accounts(session, plan), now
                )
    return count


@celery_app.task(name="reporting.plan_balances", time_limit=180, soft_time_limit=150)
def plan_balances_task():
    return plan_balances(database_engine=engine)


def run_balance(*, database_engine, redis_client, context, run_id, page):
    if not settings.ADS_SYNC_ENABLED:
        return "WAITING"
    with Session(database_engine) as session, session.begin():
        run = session.exec(
            select(ReportBalanceRun)
            .where(ReportBalanceRun.id == run_id)
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if run is None:
            return "IGNORED"
        if run.tenant_id != context.tenant_id or run.actor_id != context.actor_id:
            raise DomainError("dispatch_payload_invalid", "余额任务与冻结身份不符")
        if run.status != "ACTIVE" or run.next_page != page:
            return "IGNORED"
        route = FrozenTikTokRoute.model_validate(run.frozen_route)
        if route.channel != "OFFICIAL_API" or route.tenant_id != context.tenant_id:
            raise DomainError("api_binding_required", "余额同步需要当前租户的 API 绑定")
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=datetime.now(UTC) + timedelta(seconds=45),
        ) as gateway:
            balances, next_page = gateway.ads.read_balance_page(
                tuple(run.remaining_advertiser_ids), page=page
            )
        for advertiser, balance in balances.items():
            if advertiser not in run.remaining_advertiser_ids:
                raise DomainError("ads_response_invalid", "余额返回了冻结范围外账户")
            persist_balance(
                session,
                context=context,
                route=route,
                advertiser_id=advertiser,
                balance=balance,
            )
        run.remaining_advertiser_ids = sorted(
            set(run.remaining_advertiser_ids) - balances.keys()
        )
        if next_page is not None and run.remaining_advertiser_ids:
            # 观测、进度与下一页派发同事务；重复消息不能重写已提交页面。
            run.next_page = next_page
            _enqueue_page(session, run)
            return "CONTINUE"
        run.status = "COMPLETE"
        return "COMPLETE"


@celery_app.task(
    name=TASK_NAME,
    bind=True,
    time_limit=65,
    soft_time_limit=55,
    max_retries=None,
    acks_late=True,
    reject_on_worker_lost=True,
)
def balance_step(self, *, tenant_id, actor_id, payload, provider_attempts=0):
    validate_dispatch_payload(payload)
    if set(payload) != {"run_id", "page"}:
        raise DomainError("dispatch_payload_invalid", "余额任务参数无效")
    context = TenantContext(
        tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
    )
    try:
        with Redis.from_url(settings.REDIS_URL) as redis:
            return run_balance(
                database_engine=engine,
                redis_client=redis,
                context=context,
                run_id=UUID(payload["run_id"]),
                page=payload["page"],
            )
    except AccountAdmissionDeferred as error:
        # 额度延后不消耗读取失败次数，保留页码及尚未观测账户。
        raise self.retry(countdown=max(1, error.retry_after_ms / 1000)) from None
    except DomainError as error:
        if (
            error.code not in {"tiktok_response_error", "read_deadline_exceeded"}
            or provider_attempts >= 5
        ):
            with Session(engine) as session, session.begin():
                run = session.get(
                    ReportBalanceRun, UUID(payload["run_id"]), with_for_update=True
                )
                if (
                    run is not None
                    and run.tenant_id == context.tenant_id
                    and run.actor_id == context.actor_id
                    and run.next_page == payload["page"]
                ):
                    run.status = "FAILED"
            return "FAILED"
        raise self.retry(
            countdown=60 * 2**provider_attempts,
            kwargs={
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "payload": payload,
                "provider_attempts": provider_attempts + 1,
            },
        ) from None
