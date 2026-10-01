"""Execute one frozen advertising-management item.

The executor is deliberately item scoped.  A provider request is preceded by a
committed attempt row and fenced by both the persisted claim and the shared
Redis object lease.  A response with unknown effect is terminal until a human
reconciles it; this module never guesses that a request was not sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.integrations.tiktok.contracts.common import TRANSIENT_NOT_SENT, RemoteCallError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.management import (
    ManagementCommand,
    ManagementField,
    ManagementReceipt,
)
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.object_coordination import MutationLease, claim_mutation
from app.jobs.outbox import enqueue_after_commit
from app.modules.accounts.routing import verify_route
from app.modules.ad_management.models import (
    ManagementReceipt as ReceiptRow,
)
from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.schemas import ManagementItemPublic
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.reporting.scheduling import SyncRequest, request_sync

_CLAIM_SECONDS = 120
_TERMINAL = frozenset({"ACCEPTED", "REJECTED", "UNKNOWN", "NO_CHANGE", "CONFLICT", "UNSUPPORTED"})
_FIELD_BY_OPERATION = {
    "update_roas": "roas",
    "update_budget": "budget",
    "set_status": "status",
    "set_material_status": "material_status",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _redis() -> Redis:
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _ref(value: dict[str, Any]) -> EntityRef:
    try:
        return EntityRef(
            UUID(str(value["tenant_id"])),
            str(value["advertiser_id"]),
            value["kind"],
            str(value["remote_id"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DomainError("management_request_invalid", "冻结目标身份无效") from exc


def _material(value: dict[str, Any] | None) -> MaterialUseRef | None:
    if value is None:
        return None
    try:
        return MaterialUseRef(
            _ref(value["ad_ref"]),
            str(value["platform_material_id"]),
            value.get("ad_material_id"),
            str(value["material_type"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DomainError("management_request_invalid", "冻结素材身份无效") from exc


def _command(item: ManagementTaskItem) -> ManagementCommand:
    ref = _ref(item.ref)
    capability = item.capability or {}
    operation = capability.get("operation")
    field = _FIELD_BY_OPERATION.get(str(operation))
    if field is None:
        raise DomainError("management_operation_invalid", "冻结管理操作无效")
    original = dict(capability.get("configuration") or {})
    if capability.get("ad_type") is not None:
        original.setdefault("ad_type", capability["ad_type"])
    final = item.final_value
    if final is None:
        raise DomainError("management_request_invalid", "冻结最终值缺失")
    desired: dict[str, Any]
    if field == "roas":
        desired = {"roas_bid": final}
    elif field == "budget":
        desired = {"budget": final}
    else:
        desired = {"operation_status": final}
    return ManagementCommand(
        ref=ref,
        field=cast(ManagementField, field),
        original=original,
        desired=desired,
        ad_material_id=capability.get("ad_material_id"),
    )


def _public(item: ManagementTaskItem) -> ManagementItemPublic:
    def decimal(value: str | None) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError):
            return None

    final: Decimal | str | None = item.final_value
    if final is not None and str(final) not in {"ENABLE", "DISABLE"}:
        try:
            final = Decimal(str(final))
        except (InvalidOperation, ValueError):
            pass
    return ManagementItemPublic(
        ref=_ref(item.ref),
        material_use=_material(item.material_use),
        original_value=decimal(item.original_value),
        final_value=final,
        reason=item.reason,
        execution_result=cast(Any, item.execution_result),
        observation_state=item.observation_state,
        delivery_status=item.delivery_status,
        request_attribution=item.request_attribution,
    )


def _route(task: ManagementTask) -> FrozenTikTokRoute:
    try:
        return FrozenTikTokRoute.model_validate(task.route)
    except (TypeError, ValueError) as exc:
        raise DomainError("frozen_route_scope_mismatch", "冻结执行路由无效") from exc


def _attempts(session: Session, item: ManagementTaskItem) -> list[ManagementRequestAttempt]:
    return list(session.exec(
        select(ManagementRequestAttempt)
        .where(
            ManagementRequestAttempt.tenant_id == item.tenant_id,
            ManagementRequestAttempt.task_item_id == item.id,
        )
        .order_by(col(ManagementRequestAttempt.attempt))
    ).all())


def _can_run(
    session: Session,
    item: ManagementTaskItem,
    attempts: list[ManagementRequestAttempt],
) -> bool:
    if item.execution_result in _TERMINAL:
        return False
    if any(row.outcome == "UNKNOWN" for row in attempts):
        item.execution_result = "UNKNOWN"
        item.observation_state = "NEEDS_REVIEW"
        return False
    latest = attempts[-1] if attempts else None
    if latest is not None and latest.outcome == "ACCEPTED":
        item.execution_result = "ACCEPTED"
        return False
    if latest is not None and latest.outcome in {"NOT_SENT", "REJECTED"} and not latest.retryable:
        if latest.outcome == "NOT_SENT" and item.delivery_status == "ATTEMPT_RECORDED":
            # While the original worker still owns a live claim, its provider
            # request may be in flight.  Leave the item pending so that worker
            # can publish the receipt; after the claim expires, fail closed as
            # UNKNOWN instead of replaying a possibly accepted mutation.
            if item.claim_token is not None and item.claimed_until is not None and item.claimed_until > _now():
                return False
            # The previous worker's claim is no longer live.  Preserve a
            # durable UNKNOWN attempt and receipt before exposing the terminal
            # item state; otherwise a recovery delivery would leave only an
            # item flag with no auditable request outcome.
            latest.outcome = "UNKNOWN"
            latest.retryable = False
            latest.response = {"reason": "attempt_claim_expired"}
            session.add(latest)
            session.add(
                ReceiptRow(
                    tenant_id=item.tenant_id,
                    attempt_id=latest.id,
                    outcome="UNKNOWN",
                    request_id=latest.request_id,
                    response={"reason": "attempt_claim_expired"},
                )
            )
            item.execution_result = "UNKNOWN"
            item.observation_state = "NEEDS_REVIEW"
            item.claim_token = None
            item.claimed_until = None
        return False
    return True


def _same_frozen_value(session: Session, item: ManagementTaskItem, ref: EntityRef) -> None:
    """Verify the local observation still equals the immutable preview value."""
    capability = item.capability or {}
    if item.original_value is None:
        raise DomainError("management_observation_conflict", "冻结目标原始值缺失")
    if item.material_use is not None and capability.get("operation") == "set_material_status":
        use = _material(item.material_use)
        assert use is not None
        row = session.get(
            AdMaterialReference,
            (ref.tenant_id, ref.advertiser_id, use.ad_ref.remote_id, use.platform_material_id, use.ad_material_id, use.material_type),
        )
        if row is None:
            raise DomainError("management_observation_conflict", "冻结素材当前观测不存在")
        current = row.operation_status
        if current is None:
            raise DomainError("management_observation_conflict", "冻结素材当前值缺失")
    else:
        row = session.get(AdObject, (ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id))
        if row is None:
            # Some management dimensions have no directory row.  Capability
            # evidence cannot replace the current immutable observation.
            raise DomainError("management_observation_conflict", "冻结目标当前观测不存在")
        expected_parent = _ref(item.parent_ref) if item.parent_ref else None
        actual_parent = row.parent_ref
        if (expected_parent is None) != (actual_parent is None) or (
            expected_parent is not None
            and actual_parent is not None
            and (
                expected_parent.tenant_id != actual_parent.tenant_id
                or expected_parent.advertiser_id != actual_parent.advertiser_id
                or expected_parent.kind != actual_parent.kind
                or expected_parent.remote_id != actual_parent.remote_id
            )
        ):
            raise DomainError("management_parent_conflict", "管理目标父级关系已变化")
        expected_channel = capability.get("source_channel")
        expected_connection = capability.get("source_connection_id")
        if expected_channel and row.source_channel != expected_channel:
            raise DomainError("management_source_conflict", "目录对象来源通道已变化")
        if expected_connection and str(row.source_connection_id) != str(expected_connection):
            raise DomainError("management_source_conflict", "目录对象来源连接已变化")
        operation = str(capability.get("operation"))
        if operation == "set_status":
            current = row.operation_status
        else:
            key = "roas_bid" if operation == "update_roas" else "budget"
            current = (row.configuration or {}).get(key)
    if current is None:
        raise DomainError("management_observation_conflict", "冻结目标当前值缺失")
    if str(current) != str(item.original_value):
        raise DomainError("management_original_changed", "管理目标原始值已变化")


def _update_task(session: Session, task_id: UUID, tenant_id: UUID) -> None:
    task = session.exec(
        select(ManagementTask).where(
            ManagementTask.id == task_id,
            ManagementTask.tenant_id == tenant_id,
        ).with_for_update()
    ).one_or_none()
    if task is None:
        return
    rows = session.exec(
        select(ManagementTaskItem).where(
            ManagementTaskItem.task_id == task.id,
            ManagementTaskItem.tenant_id == tenant_id,
        )
    ).all()
    counts = dict(task.counts or {})
    for key in ("accepted", "rejected", "unknown", "not_sent", "pending", "unsupported"):
        counts[key] = sum(
            1
            for row in rows
            if row.execution_result == {
                "accepted": "ACCEPTED", "rejected": "REJECTED", "unknown": "UNKNOWN",
                "not_sent": "NOT_SENT", "pending": "PENDING", "unsupported": "UNSUPPORTED",
            }[key]
        )
    task.counts = counts
    statuses = {row.execution_result for row in rows}
    if "UNKNOWN" in statuses:
        task.status = "NEEDS_REVIEW"
    elif "PENDING" in statuses or "NOT_SENT" in statuses:
        task.status = "RUNNING"
    elif statuses and statuses <= {"ACCEPTED", "NO_CHANGE"}:
        task.status = "SUCCEEDED"
    elif statuses:
        task.status = "PARTIAL"
    task.updated_at = _now()
    session.add(task)


def _payload(command: ManagementCommand) -> dict[str, Any]:
    return {
        "operation": command.operation,
        "entity_kind": command.entity_kind,
        "ref": {
            "advertiser_id": command.ref.advertiser_id,
            "kind": command.ref.kind,
            "remote_id": command.ref.remote_id,
        },
        "original": dict(command.original),
        "desired": dict(command.desired),
    }


def _evidence(receipt: ManagementReceipt) -> dict[str, Any]:
    evidence = receipt.evidence
    if evidence is None:
        return {}
    return {
        "request_id": evidence.request_id,
        "mcp_request_id": evidence.mcp_request_id,
        "remote_task_id": evidence.remote_task_id,
        "remote_code": evidence.remote_code,
    }


def _persist_outcome(
    database_engine: Engine,
    *,
    item_id: UUID,
    token: UUID,
    generation: int,
    attempt_no: int,
    outcome: str,
    request_id: str | None,
    retryable: bool,
    response: dict[str, Any],
    lease: MutationLease,
    context: TenantContext,
    route: FrozenTikTokRoute,
) -> bool:
    with Session(database_engine) as session, session.begin():
        item = session.exec(
            select(ManagementTaskItem).where(ManagementTaskItem.id == item_id).with_for_update()
        ).one_or_none()
        if item is None or item.claim_token != token or item.claim_generation != generation:
            return False
        # Provider acceptance is only written by the current fenced owner.  A
        # stale owner leaves the durable attempt untouched for explicit review.
        lease.fence()
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=_ref(item.ref).advertiser_id,
            capability="ads_manage",
            operation=(item.capability or {}).get("operation"),
            entity_kind=(item.capability or {}).get("entity_kind"),
        )
        attempt = session.exec(
            select(ManagementRequestAttempt)
            .where(
                ManagementRequestAttempt.tenant_id == item.tenant_id,
                ManagementRequestAttempt.task_item_id == item.id,
                ManagementRequestAttempt.attempt == attempt_no,
            )
            .with_for_update()
        ).one_or_none()
        if attempt is None:
            # Generation is the attempt identity for this worker; a missing row
            # is a fence failure rather than permission to create a duplicate.
            return False
        attempt.outcome = outcome
        attempt.request_id = request_id
        attempt.retryable = retryable
        attempt.response = response
        session.add(attempt)
        session.add(
            ReceiptRow(
                tenant_id=item.tenant_id,
                attempt_id=attempt.id,
                outcome=outcome,
                request_id=request_id,
                response=response,
            )
        )
        item.execution_result = outcome
        item.delivery_status = "DELIVERED" if outcome == "ACCEPTED" else outcome
        item.observation_state = "ACCEPTED" if outcome == "ACCEPTED" else ("NEEDS_REVIEW" if outcome == "UNKNOWN" else None)
        item.request_attribution = request_id
        item.claim_token = None
        item.claimed_until = None
        session.add(item)
        _update_task(session, item.task_id, item.tenant_id)
    return True


def _refresh_after_commit(
    database_engine: Engine,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    ref: EntityRef,
) -> None:
    # Refresh is a separate durable transaction.  A read permission change after
    # a successful write must not rewrite the already accepted management receipt.
    with Session(database_engine) as session, session.begin():
        request_sync(
            session,
            context=context,
            request=SyncRequest(
                route=route,
                advertiser_ids=(ref.advertiser_id,),
                scope="targeted",
                start_date=None,
                end_date=None,
                refs=(ref,),
            ),
        )


def _set_refresh_state(
    database_engine: Engine,
    item_id: UUID,
    *,
    pending: bool,
    error: str | None = None,
) -> None:
    """Persist targeted-refresh progress independently of the write receipt."""
    with Session(database_engine) as session, session.begin():
        item = session.exec(
            select(ManagementTaskItem).where(ManagementTaskItem.id == item_id).with_for_update()
        ).one_or_none()
        if item is None or item.execution_result != "ACCEPTED":
            return
        item.observation_state = "REFRESH_PENDING" if pending else "ACCEPTED"
        if pending:
            item.reason = error or "targeted_refresh_failed"
            task = session.get(ManagementTask, item.task_id)
            if task is not None:
                enqueue_after_commit(
                    session,
                    context=TenantContext(
                        tenant_id=task.tenant_id,
                        actor_id=task.actor_id,
                        role="operator",
                    ),
                    task_name="ad_management.execute",
                    task_key=f"ad_management.refresh:{item.id}:{uuid4()}",
                    payload={"task_id": str(task.id), "generation": 1},
                )
        elif item.reason == "targeted_refresh_failed":
            item.reason = None
        session.add(item)


def _retry_targeted_refresh(database_engine: Engine, item_id: UUID) -> None:
    """Retry a durable post-write refresh without replaying the mutation."""
    with Session(database_engine) as session:
        item = session.exec(
            select(ManagementTaskItem).where(ManagementTaskItem.id == item_id).with_for_update()
        ).one_or_none()
        if item is None or item.execution_result != "ACCEPTED":
            return
        task = session.get(ManagementTask, item.task_id)
        if task is None:
            return
        route = _route(task)
        context = TenantContext(tenant_id=task.tenant_id, actor_id=task.actor_id, role="operator")
        ref = _ref(item.ref)
        session.commit()
    try:
        _refresh_after_commit(database_engine, context=context, route=route, ref=ref)
    except Exception:
        _set_refresh_state(database_engine, item_id, pending=True)
    else:
        _set_refresh_state(database_engine, item_id, pending=False)


def _mark_unknown_after_persist_failure(
    database_engine: Engine,
    *,
    item_id: UUID,
    token: UUID,
    generation: int,
    attempt_no: int,
) -> None:
    """Turn this owner's committed pre-attempt into UNKNOWN on receipt failure."""
    try:
        with Session(database_engine) as session, session.begin():
            item = session.exec(select(ManagementTaskItem).where(ManagementTaskItem.id == item_id).with_for_update()).one_or_none()
            if item is None or item.claim_token != token or item.claim_generation != generation:
                return
            attempt = session.exec(
                select(ManagementRequestAttempt)
                .where(
                    ManagementRequestAttempt.tenant_id == item.tenant_id,
                    ManagementRequestAttempt.task_item_id == item.id,
                    ManagementRequestAttempt.attempt == attempt_no,
                )
                .with_for_update()
            ).one_or_none()
            if attempt is None or attempt.outcome != "NOT_SENT":
                return
            attempt.outcome = "UNKNOWN"
            attempt.retryable = False
            attempt.response = {"reason": "receipt_commit_failed"}
            session.add(attempt)
            session.add(ReceiptRow(tenant_id=item.tenant_id, attempt_id=attempt.id, outcome="UNKNOWN", response={"reason": "receipt_commit_failed"}))
            item.execution_result = "UNKNOWN"
            item.observation_state = "NEEDS_REVIEW"
            item.claim_token = None
            item.claimed_until = None
            session.add(item)
            _update_task(session, item.task_id, item.tenant_id)
    except Exception:
        # A second database failure leaves the durable pre-attempt in place;
        # its ATTEMPT_RECORDED marker still blocks an automatic replay.
        return


def _mark_unknown_after_fence_failure(
    database_engine: Engine,
    *,
    item_id: UUID,
    token: UUID,
    generation: int,
    attempt_no: int,
) -> None:
    """Fail closed after a sent request cannot pass its receipt fence.

    The token and generation predicates ensure an old worker cannot overwrite
    a newer claim that has already taken ownership of the item.
    """
    try:
        with Session(database_engine) as session, session.begin():
            item = session.exec(
                select(ManagementTaskItem)
                .where(ManagementTaskItem.id == item_id)
                .with_for_update()
            ).one_or_none()
            if item is None or item.claim_token != token or item.claim_generation != generation:
                return
            attempt = session.exec(
                select(ManagementRequestAttempt)
                .where(
                    ManagementRequestAttempt.tenant_id == item.tenant_id,
                    ManagementRequestAttempt.task_item_id == item.id,
                    ManagementRequestAttempt.attempt == attempt_no,
                )
                .with_for_update()
            ).one_or_none()
            if attempt is None or attempt.outcome != "NOT_SENT":
                return
            attempt.outcome = "UNKNOWN"
            attempt.retryable = False
            attempt.response = {"reason": "receipt_fence_failed"}
            session.add(attempt)
            session.add(
                ReceiptRow(
                    tenant_id=item.tenant_id,
                    attempt_id=attempt.id,
                    outcome="UNKNOWN",
                    response={"reason": "receipt_fence_failed"},
                )
            )
            item.execution_result = "UNKNOWN"
            item.observation_state = "NEEDS_REVIEW"
            item.claim_token = None
            item.claimed_until = None
            session.add(item)
            _update_task(session, item.task_id, item.tenant_id)
    except Exception:
        # Preserve the committed pre-attempt when this recovery transaction is
        # itself unavailable; the next delivery will fail closed after expiry.
        return


def execute_item(database_engine: Engine, item_id: UUID) -> None:
    """Execute one item; failures are persisted per item and never abort siblings."""
    redis_client = _redis()
    lease: MutationLease | None = None
    owner = uuid4()
    try:
        with Session(database_engine) as session:
            item = session.exec(select(ManagementTaskItem).where(ManagementTaskItem.id == item_id).with_for_update()).one_or_none()
            if item is None:
                raise DomainError("management_item_not_found", "管理任务项不存在")
            task = session.exec(select(ManagementTask).where(ManagementTask.id == item.task_id, ManagementTask.tenant_id == item.tenant_id)).one_or_none()
            if task is None:
                raise DomainError("management_task_not_found", "管理任务不存在")
            if item.execution_result == "ACCEPTED" and item.observation_state == "REFRESH_PENDING":
                session.commit()
                _retry_targeted_refresh(database_engine, item_id)
                return
            attempts = _attempts(session, item)
            if not _can_run(session, item, attempts):
                _update_task(session, task.id, task.tenant_id)
                session.commit()
                return
            route = _route(task)
            context = TenantContext(tenant_id=task.tenant_id, actor_id=task.actor_id, role="operator")
            ref = _ref(item.ref)
            command = _command(item)
            if route.tenant_id != item.tenant_id or ref.tenant_id != item.tenant_id or route.channel not in {"OFFICIAL_API", "OFFICIAL_MCP"}:
                raise DomainError("frozen_route_scope_mismatch", "冻结路由与管理目标不一致")
            if (item.capability or {}).get("route") != route.model_dump(mode="json"):
                raise DomainError("frozen_route_changed", "任务项冻结路由已变化")
            # claim_mutation shares the lock domain with build.disable_adgroup;
            # Redis failure is a per-item failure, never a whole-task abort.
            lease = claim_mutation(session, (ref,), owner, redis_client=redis_client, lease_seconds=_CLAIM_SECONDS)
            generation = item.claim_generation + (1 if attempts else 0)
            item.claim_generation = generation
            item.claim_token = owner
            item.claimed_until = _now() + timedelta(seconds=_CLAIM_SECONDS)
            task.status = "RUNNING"
            # Claim and pre-attempt are one short transaction.  A duplicate
            # delivery cannot observe an unclaimed item between these writes and
            # open a second provider request.
            attempt_no = max([row.attempt for row in attempts], default=0) + 1
            session.add(
                ManagementRequestAttempt(
                    tenant_id=item.tenant_id,
                    task_item_id=item.id,
                    attempt=attempt_no,
                    outcome="NOT_SENT",
                    payload=_payload(command),
                    response={},
                    retryable=False,
                )
            )
            item.delivery_status = "ATTEMPT_RECORDED"
            session.add_all([item, task])
            session.flush()
            session.commit()
            # Attempt row identity is separate from Redis claim generation.

        assert lease is not None
        lease.fence(redis_client)
        with Session(database_engine) as check:
            current = check.exec(select(ManagementTaskItem).where(ManagementTaskItem.id == item_id)).one()
            task = check.get(ManagementTask, current.task_id)
            assert task is not None
            route = _route(task)
            context = TenantContext(tenant_id=task.tenant_id, actor_id=task.actor_id, role="operator")
            ref = _ref(current.ref)
            verify_route(check, context=context, route=route, advertiser_id=ref.advertiser_id, capability="ads_manage", operation=(current.capability or {}).get("operation"), entity_kind=(current.capability or {}).get("entity_kind"))
            _same_frozen_value(check, current, ref)
            command = _command(current)

        def before_provider_request() -> None:
            """Re-fence and re-read the frozen object at each adapter send gate."""
            lease.fence(redis_client)
            with Session(database_engine) as preflight:
                latest = preflight.exec(
                    select(ManagementTaskItem).where(ManagementTaskItem.id == item_id)
                ).one_or_none()
                if latest is None:
                    raise DomainError("management_item_not_found", "管理任务项不存在")
                latest_task = preflight.get(ManagementTask, latest.task_id)
                if latest_task is None:
                    raise DomainError("management_task_not_found", "管理任务不存在")
                latest_route = _route(latest_task)
                if latest_route != route or (latest.capability or {}).get("route") != route.model_dump(mode="json"):
                    raise DomainError("frozen_route_changed", "任务项冻结路由已变化")
                if latest.claim_token != owner or latest.claim_generation != generation:
                    raise DomainError("management_claim_stale", "管理任务领取已过期")
                latest_ref = _ref(latest.ref)
                verify_route(
                    preflight,
                    context=context,
                    route=route,
                    advertiser_id=latest_ref.advertiser_id,
                    capability="ads_manage",
                    operation=(latest.capability or {}).get("operation"),
                    entity_kind=(latest.capability or {}).get("entity_kind"),
                )
                _same_frozen_value(preflight, latest, latest_ref)

        provider_called = False
        try:
            deadline = _now() + timedelta(seconds=45)
            with open_tiktok_gateway(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                route=route,
                task_deadline=deadline,
                ad_type=(command.original.get("ad_type") if command.original else None),
                before_request=before_provider_request,
            ) as gateway:
                lease.fence(redis_client)
                provider_called = True
                receipt = gateway.management.apply(command)
        except RemoteCallError as error:
            outcome = "UNKNOWN" if error.effect == "UNKNOWN" else "NOT_SENT"
            receipt = ManagementReceipt(outcome, error.evidence.request_id or error.evidence.mcp_request_id, outcome == "NOT_SENT" and error.code in TRANSIENT_NOT_SENT, error.evidence)
        except DomainError as error:
            outcome = "UNKNOWN" if provider_called else "NOT_SENT"
            receipt = ManagementReceipt(outcome, None, outcome == "NOT_SENT" and error.code in TRANSIENT_NOT_SENT)
        except Exception:
            outcome = "UNKNOWN" if provider_called else "NOT_SENT"
            receipt = ManagementReceipt(outcome, None, False)

        # The attempt number is looked up from SQL after the provider call.  It is
        # never inferred from Redis generation, preserving retries after restart.
        with Session(database_engine) as read:
            attempt = read.exec(select(ManagementRequestAttempt).where(ManagementRequestAttempt.task_item_id == item_id).order_by(col(ManagementRequestAttempt.attempt).desc())).first()
            assert attempt is not None
            attempt_no = attempt.attempt
        try:
            persisted = _persist_outcome(database_engine, item_id=item_id, token=owner, generation=generation, attempt_no=attempt_no, outcome=receipt.outcome, request_id=receipt.request_id, retryable=receipt.retryable, response=_evidence(receipt), lease=lease, context=context, route=route)
        except DomainError:
            # A stale lease or route/permission generation is itself a writeback
            # fence.  When the provider was reached, fail closed with a durable
            # UNKNOWN guarded by this worker's claim token and generation.
            if provider_called:
                _mark_unknown_after_fence_failure(
                    database_engine,
                    item_id=item_id,
                    token=owner,
                    generation=generation,
                    attempt_no=attempt_no,
                )
            persisted = False
        except Exception:
            _mark_unknown_after_persist_failure(
                database_engine,
                item_id=item_id,
                token=owner,
                generation=generation,
                attempt_no=attempt_no,
            )
            persisted = False
        if persisted and receipt.outcome == "ACCEPTED":
            try:
                _refresh_after_commit(database_engine, context=context, route=route, ref=ref)
            except Exception:
                # Keep the accepted receipt while recording a durable refresh
                # retry state; a later management delivery only refreshes.
                _set_refresh_state(database_engine, item_id, pending=True)
            else:
                _set_refresh_state(database_engine, item_id, pending=False)
    except DomainError as error:
        # Claim/preflight failures are isolated to this item and are recorded as
        # a non-sent, non-retryable attempt where possible.
        with Session(database_engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            if item is not None and item.execution_result == "PENDING":
                item.reason = error.code
                item.execution_result = "NOT_SENT"
                item.claim_token = None
                item.claimed_until = None
                item.delivery_status = error.code
                item.observation_state = "NEEDS_REVIEW" if error.code.startswith("route_") else None
                session.add(item)
                _update_task(session, item.task_id, item.tenant_id)
                session.commit()
    finally:
        if lease is not None:
            lease.release(redis_client)
        redis_client.close()


__all__ = ["execute_item"]
