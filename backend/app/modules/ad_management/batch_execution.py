"""一个物理请求的领取、围栏与逐成员审计；等待平台时不持SQL事务。"""

from datetime import timedelta
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.adapters.management_batches import (
    batch_endpoint,
    plan_batches,
)
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.management import ManagementReceipt
from app.integrations.tiktok.object_coordination import claim_mutation
from app.modules.accounts.routing import verify_route
from app.modules.ad_management import execution as ex
from app.modules.ad_management.batch_models import (
    ManagementPhysicalMember,
    ManagementPhysicalRequest,
)
from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)


def execute_batch(database_engine, item_ids: tuple[UUID, ...]) -> bool:
    """返回False仅表示锁竞争尚未发送，调用方持久重排；不能消耗重试预算。"""
    if not item_ids or len(set(item_ids)) != len(item_ids):
        raise ValueError("physical batch members must be nonempty and unique")
    redis_client = ex._redis()
    lease = None
    owner = uuid4()
    prepared = []
    physical_id = None
    try:
        with Session(database_engine) as session:
            scopes = session.exec(
                select(ManagementTaskItem.task_id, ManagementTaskItem.tenant_id)
                .where(col(ManagementTaskItem.id).in_(item_ids))
                .distinct()
            ).all()
            if len(scopes) != 1:
                raise DomainError("frozen_route_scope_mismatch", "批次不能跨任务或租户")
            task = session.exec(
                select(ManagementTask)
                .where(
                    ManagementTask.id == scopes[0][0],
                    ManagementTask.tenant_id == scopes[0][1],
                )
                .with_for_update()
            ).one_or_none()
            if task is None or task.status in {"CANCELLED", "PREPARING", "FAILED"}:
                return True
            rows = session.exec(
                select(ManagementTaskItem)
                .where(col(ManagementTaskItem.id).in_(item_ids))
                .order_by(ManagementTaskItem.id)
                .with_for_update()
            ).all()
            if (
                len(rows) != len(item_ids)
                or len({(r.tenant_id, r.task_id) for r in rows}) != 1
            ):
                raise DomainError("frozen_route_scope_mismatch", "批次不能跨任务或租户")
            task_id = task.id
            route = ex._route(task)
            context = TenantContext(task.tenant_id, task.actor_id, "operator")
            eligible = []
            for item in sorted(rows, key=lambda r: r.position):
                attempts = ex._attempts(session, item)
                if not ex._can_run(session, item, attempts):
                    session.add(item)
                    continue
                if (item.capability or {}).get("route") != route.model_dump(
                    mode="json"
                ):
                    raise DomainError("frozen_route_changed", "批次成员冻结路由已变化")
                ref = ex._ref(item.ref)
                if ref.tenant_id != task.tenant_id or route.tenant_id != task.tenant_id:
                    raise DomainError(
                        "frozen_route_scope_mismatch", "批次成员租户不一致"
                    )
                # 个别对象失效只跳过该对象；整批共享的路由/授权门禁仍在发送前重验。
                try:
                    verify_route(
                        session,
                        context=context,
                        route=route,
                        advertiser_id=ref.advertiser_id,
                        capability="ads_manage",
                        operation=item.capability.get("operation"),
                        entity_kind=ref.kind,
                    )
                    ex._same_frozen_value(session, item, ref)
                    command = ex._command(item)
                except DomainError as error:
                    item.execution_result = (
                        "CONFLICT"
                        if error.code == "management_original_changed"
                        else "NOT_SENT"
                    )
                    item.reason = error.code
                    item.delivery_status = error.code
                    session.add(item)
                    continue
                eligible.append((item, attempts, command))
            if not eligible:
                ex._update_task(session, task.id, task.tenant_id)
                session.commit()
                return True
            commands = tuple(entry[2] for entry in eligible)
            if plan_batches(commands) != (commands,):
                raise DomainError(
                    "management_request_invalid", "批次混入不同接口或动作"
                )
            try:
                lease = claim_mutation(
                    session,
                    tuple(c.ref for c in commands),
                    owner,
                    redis_client=redis_client,
                    lease_seconds=ex._CLAIM_SECONDS,
                )
            except DomainError as error:
                if error.code == "mutation_busy":
                    return False
                raise
            physical = ManagementPhysicalRequest(
                tenant_id=task.tenant_id,
                task_id=task.id,
                endpoint=batch_endpoint(commands[0]),
                member_count=len(commands),
            )
            session.add(physical)
            session.flush()
            physical_id = physical.id
            for item, attempts, command in eligible:
                generation = item.claim_generation + (1 if attempts else 0)
                item.claim_generation = generation
                item.claim_token = owner
                item.claimed_until = ex._now() + timedelta(seconds=ex._CLAIM_SECONDS)
                item.execution_result = "PENDING"
                item.delivery_status = "ATTEMPT_RECORDED"
                attempt_no = max((a.attempt for a in attempts), default=0) + 1
                attempt = ManagementRequestAttempt(
                    tenant_id=item.tenant_id,
                    task_item_id=item.id,
                    attempt=attempt_no,
                    outcome="NOT_SENT",
                    payload=ex._payload(command),
                    retryable=False,
                )
                session.add_all([item, attempt])
                session.flush()
                session.add(
                    ManagementPhysicalMember(
                        tenant_id=item.tenant_id,
                        task_id=task.id,
                        batch_id=physical.id,
                        task_item_id=item.id,
                        attempt_id=attempt.id,
                    )
                )
                prepared.append((item.id, generation, attempt_no, command))
            task.status = "RUNNING"
            session.add(task)
            from app.modules.ad_management.recovery_dispatch import schedule_wakeup

            schedule_wakeup(
                session,
                task,
                max(entry[0].claimed_until for entry in eligible)
                + timedelta(seconds=1),
                kind="claim",
                quantum=30,
            )
            session.commit()

        def before_request():
            lease.fence(redis_client)
            with Session(database_engine) as check:
                current_task = check.get(ManagementTask, task_id)
                if (
                    current_task is None
                    or current_task.status == "CANCELLED"
                    or ex._route(current_task) != route
                ):
                    raise DomainError(
                        "management_task_cancelled", "任务已取消或路由已变化"
                    )
                current = check.exec(
                    select(ManagementTaskItem).where(
                        col(ManagementTaskItem.id).in_([r[0] for r in prepared])
                    )
                ).all()
                by_id = {r.id: r for r in current}
                for item_id, generation, _, command in prepared:
                    item = by_id.get(item_id)
                    if (
                        item is None
                        or item.claim_token != owner
                        or item.claim_generation != generation
                        or (item.capability or {}).get("route")
                        != route.model_dump(mode="json")
                    ):
                        raise DomainError("management_claim_stale", "批次领取已失效")
                    verify_route(
                        check,
                        context=context,
                        route=route,
                        advertiser_id=command.ref.advertiser_id,
                        capability="ads_manage",
                        operation=command.operation,
                        entity_kind=command.ref.kind,
                    )
                    ex._same_frozen_value(check, item, command.ref)
            lease.fence(redis_client)

        called = False
        try:
            before_request()
            with ex.open_tiktok_gateway(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                route=route,
                task_deadline=ex._now() + timedelta(seconds=45),
                before_request=before_request,
            ) as gateway:
                called = True
                receipts = gateway.management.apply_batch(commands)
            if len(receipts) != len(prepared):
                raise ValueError("batch receipt membership changed")
        except RemoteCallError as error:
            outcome = "UNKNOWN" if error.effect == "UNKNOWN" else "NOT_SENT"
            receipts = tuple(
                ManagementReceipt(
                    outcome,
                    error.evidence.request_id,
                    outcome == "NOT_SENT",
                    error.evidence,
                    reason=error.code,
                    retry_after_ms=error.retry_after_ms,
                )
                for _ in prepared
            )
        except DomainError as error:
            receipts = tuple(
                ManagementReceipt(
                    "UNKNOWN" if called else "NOT_SENT", None, False, reason=error.code
                )
                for _ in prepared
            )
        except Exception:
            receipts = tuple(
                ManagementReceipt(
                    "UNKNOWN" if called else "NOT_SENT",
                    None,
                    False,
                    reason="management_result_unknown"
                    if called
                    else "management_preflight_failed",
                )
                for _ in prepared
            )
        for (item_id, generation, attempt_no, command), receipt in zip(
            prepared, receipts, strict=True
        ):
            try:
                persisted = ex._persist_outcome(
                    database_engine,
                    item_id=item_id,
                    token=owner,
                    generation=generation,
                    attempt_no=attempt_no,
                    outcome=receipt.outcome,
                    request_id=receipt.request_id,
                    retryable=receipt.retryable,
                    response={
                        **ex._evidence(receipt),
                        "physical_batch_id": str(physical_id),
                    },
                    lease=lease,
                    context=context,
                    route=route,
                    update_summary=False,
                    enqueue_retry=True,
                )
            except DomainError:
                if called:
                    ex._mark_unknown_after_fence_failure(
                        database_engine,
                        item_id=item_id,
                        token=owner,
                        generation=generation,
                        attempt_no=attempt_no,
                    )
                persisted = False
            except Exception:
                ex._mark_unknown_after_persist_failure(
                    database_engine,
                    item_id=item_id,
                    token=owner,
                    generation=generation,
                    attempt_no=attempt_no,
                )
                persisted = False
            if called and not persisted:
                ex._mark_unknown_after_fence_failure(
                    database_engine,
                    item_id=item_id,
                    token=owner,
                    generation=generation,
                    attempt_no=attempt_no,
                )
            if persisted and receipt.outcome == "ACCEPTED":
                try:
                    ex._refresh_after_commit(
                        database_engine, context=context, route=route, ref=command.ref
                    )
                except Exception:
                    ex._set_refresh_state(database_engine, item_id, pending=True)
        with Session(database_engine) as session, session.begin():
            physical = session.get(ManagementPhysicalRequest, physical_id)
            if physical:
                # 逐成员attempt才是恢复依据，此状态仅为物理请求摘要。
                attempts = session.exec(
                    select(ManagementRequestAttempt.outcome)
                    .join(
                        ManagementPhysicalMember,
                        ManagementPhysicalMember.attempt_id
                        == ManagementRequestAttempt.id,
                    )
                    .where(ManagementPhysicalMember.batch_id == physical_id)
                ).all()
                physical.status = "UNKNOWN" if "UNKNOWN" in attempts else "COMPLETE"
                session.add(physical)
            ex._update_task(session, task_id, context.tenant_id)
        return True
    finally:
        if lease is not None:
            lease.release(redis_client)
        redis_client.close()
