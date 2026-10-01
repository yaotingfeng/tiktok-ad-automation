"""Scoped task queries and human recovery actions for ad management."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.jobs.outbox import enqueue_after_commit
from app.modules.accounts.routing import verify_route
from app.modules.ad_management.execution import _public as _item_public
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementPreviewItem,
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.reconciliation import reconcile_item
from app.modules.ad_management.schemas import (
    ManagementAttemptPublic,
    ManagementCounts,
    ManagementItemPublic,
    ManagementPreviewPublic,
    ManagementTaskPublic,
)
from app.modules.ad_management.submissions import (
    MANAGEMENT_DISPATCH_TASK,
    _idempotency_lock,
)
from app.modules.ads.models import AdObject
from app.modules.tenants.permissions import require_tenant


def _public(
    task: ManagementTask,
    *,
    items: tuple[ManagementItemPublic, ...] = (),
) -> ManagementTaskPublic:
    return ManagementTaskPublic(
        task_id=task.id,
        bc_id=task.bc_id,
        status=cast(Any, task.status),
        counts=ManagementCounts.model_validate(task.counts or {}),
        items=items,
    )


def _route(task: ManagementTask) -> FrozenTikTokRoute:
    try:
        return FrozenTikTokRoute.model_validate(task.route)
    except (TypeError, ValueError) as exc:
        raise DomainError("frozen_route_changed", "任务冻结路由无效") from exc


def _scoped_task(
    session: Session,
    context: TenantContext,
    bc_id: str,
    task_id: UUID,
    *,
    action: str = "read",
) -> ManagementTask:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action=action,
    )
    task = session.exec(
        select(ManagementTask).where(
            ManagementTask.id == task_id,
            ManagementTask.tenant_id == context.tenant_id,
            ManagementTask.bc_id == bc_id,
        )
    ).one_or_none()
    if task is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    route = _route(task)
    refs = session.exec(
        select(ManagementTaskItem.ref).where(
            ManagementTaskItem.task_id == task.id,
            ManagementTaskItem.tenant_id == context.tenant_id,
        )
    ).all()
    capability_name = "ads_manage" if action == "ads_manage" else "read"
    try:
        for value in refs:
            advertiser_id = value.get("advertiser_id") if isinstance(value, dict) else None
            if not isinstance(advertiser_id, str):
                raise DomainError("management_request_invalid", "任务目标身份无效")
            item = session.exec(
                select(ManagementTaskItem).where(
                    ManagementTaskItem.task_id == task.id,
                    ManagementTaskItem.tenant_id == context.tenant_id,
                    ManagementTaskItem.ref == value,
                )
            ).first()
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=advertiser_id,
                capability=capability_name,
                operation=(item.capability or {}).get("operation") if item else None,
                entity_kind=(item.capability or {}).get("entity_kind") if item else None,
            )
    except DomainError as exc:
        raise DomainError("management_task_not_found", "管理任务不存在") from exc
    return task


def get_task(
    session: Session, context: TenantContext, bc_id: str, task_id: UUID
) -> ManagementTaskPublic:
    task = _scoped_task(session, context, bc_id, task_id)
    rows = session.exec(
        select(ManagementTaskItem)
        .where(
            ManagementTaskItem.task_id == task.id,
            ManagementTaskItem.tenant_id == context.tenant_id,
        )
        .order_by(col(ManagementTaskItem.position))
    ).all()
    public_items: list[ManagementItemPublic] = []
    for row in rows:
        attempts = session.exec(
            select(ManagementRequestAttempt)
            .where(
                ManagementRequestAttempt.task_item_id == row.id,
                ManagementRequestAttempt.tenant_id == context.tenant_id,
            )
            .order_by(col(ManagementRequestAttempt.attempt))
        ).all()
        public_items.append(
            _item_public(row).model_copy(
                update={
                    "attempts": tuple(
                        ManagementAttemptPublic(
                            attempt=attempt.attempt,
                            request_at=attempt.request_at,
                            outcome=cast(Any, attempt.outcome),
                            request_id=attempt.request_id,
                            retryable=attempt.retryable,
                        )
                        for attempt in attempts
                    )
                }
            )
        )
    return _public(task, items=tuple(public_items))


def list_tasks(
    session: Session,
    context: TenantContext,
    bc_id: str,
    *,
    cursor: str | None,
    limit: int,
) -> Page[ManagementTaskPublic]:
    if not 1 <= limit <= 100:
        raise DomainError("invalid_cursor", "分页大小无效")
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="read",
    )
    statement = select(ManagementTask).where(
        ManagementTask.tenant_id == context.tenant_id,
        ManagementTask.bc_id == bc_id,
    )
    if cursor:
        try:
            cursor_id = UUID(cursor)
        except ValueError as exc:
            raise DomainError("invalid_cursor", "分页游标无效") from exc
        statement = statement.where(ManagementTask.id > cursor_id)
    # Verify account visibility in bounded batches.  A revoked account can be
    # interleaved with usable rows, so stop only after the full candidate scan;
    # each query and the retained page remain bounded rather than loading the BC
    # task history into one list.
    batch_size = max(limit + 1, 50)
    last_id = cursor_id if cursor else None
    visible: list[ManagementTask] = []
    total = 0
    while True:
        batch_statement = statement
        if last_id is not None:
            batch_statement = batch_statement.where(ManagementTask.id > last_id)
        candidates = session.exec(
            batch_statement.order_by(col(ManagementTask.id)).limit(batch_size)
        ).all()
        if not candidates:
            break
        for row in candidates:
            last_id = row.id
            try:
                _scoped_task(session, context, bc_id, row.id)
            except DomainError:
                continue
            total += 1
            if len(visible) < limit + 1:
                visible.append(row)
        if len(candidates) < batch_size:
            break
    rows = visible
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return Page(
        items=[_public(row) for row in rows[:limit]],
        next_cursor=next_cursor,
        total=total,
    )


def cancel_task(
    session: Session, context: TenantContext, task_id: UUID
) -> ManagementTaskPublic:
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="ads_manage",
    )
    task = session.exec(
        select(ManagementTask)
        .where(
            ManagementTask.id == task_id,
            ManagementTask.tenant_id == context.tenant_id,
        )
        .with_for_update()
    ).one_or_none()
    if task is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    _scoped_task(session, context, task.bc_id, task.id, action="ads_manage")
    if task.status in {"CANCELLED", "SUCCEEDED", "FAILED", "PARTIAL", "NEEDS_REVIEW"}:
        raise DomainError("management_cancel_not_allowed", "任务当前不可取消")
    rows = session.exec(
        select(ManagementTaskItem)
        .where(
            ManagementTaskItem.task_id == task.id,
            ManagementTaskItem.tenant_id == context.tenant_id,
        )
        .with_for_update()
    ).all()
    for item in rows:
        if item.execution_result == "PENDING" and item.claim_token is None:
            item.execution_result = "CANCELLED"
            item.delivery_status = "CANCELLED"
            item.reason = "cancelled"
            session.add(item)
    task.status = "CANCELLED"
    session.add(task)
    session.flush()
    return _public(task)


def retry_task(
    session: Session,
    context: TenantContext,
    task_id: UUID,
    idempotency_key: UUID,
) -> ManagementTaskPublic:
    if not isinstance(idempotency_key, UUID):
        raise DomainError("management_retry_invalid", "重试需要幂等键")
    source = session.exec(
        select(ManagementTask)
        .where(
            ManagementTask.id == task_id,
            ManagementTask.tenant_id == context.tenant_id,
        )
    ).one_or_none()
    if source is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    _scoped_task(session, context, source.bc_id, source.id, action="ads_manage")
    _idempotency_lock(session, context, idempotency_key)
    existing = session.exec(
        select(ManagementTask)
        .where(
            ManagementTask.tenant_id == context.tenant_id,
            ManagementTask.actor_id == context.actor_id,
            ManagementTask.idempotency_key == idempotency_key,
        )
    ).one_or_none()
    if existing is not None:
        if existing.digest != source.digest or existing.preview_id != source.preview_id:
            raise DomainError("idempotency_conflict", "幂等键已绑定其他任务")
        return _public(existing)
    items = session.exec(
        select(ManagementTaskItem)
        .where(
            ManagementTaskItem.task_id == source.id,
            ManagementTaskItem.tenant_id == context.tenant_id,
        )
        .order_by(col(ManagementTaskItem.position))
    ).all()
    eligible: list[ManagementTaskItem] = []
    for item in items:
        if item.execution_result != "NOT_SENT" or item.delivery_status == "CANCELLED":
            continue
        latest = session.exec(
            select(ManagementRequestAttempt)
            .where(
                ManagementRequestAttempt.tenant_id == item.tenant_id,
                ManagementRequestAttempt.task_item_id == item.id,
            )
            .order_by(col(ManagementRequestAttempt.attempt).desc())
        ).first()
        if latest is None or latest.retryable:
            eligible.append(item)
    if not eligible:
        raise DomainError("management_retry_not_allowed", "没有明确未发送的任务项可重试")
    task = ManagementTask(
        tenant_id=source.tenant_id,
        bc_id=source.bc_id,
        actor_id=context.actor_id,
        preview_id=source.preview_id,
        idempotency_key=idempotency_key,
        digest=source.digest,
        route=dict(source.route),
        status="QUEUED",
        counts=dict(source.counts),
    )
    session.add(task)
    session.flush()
    for position, old in enumerate(eligible):
        session.add(
            ManagementTaskItem(
                tenant_id=old.tenant_id,
                task_id=task.id,
                preview_item_id=old.preview_item_id,
                preview_id=old.preview_id,
                position=position,
                ref=dict(old.ref),
                material_use=dict(old.material_use) if old.material_use else None,
                original_value=old.original_value,
                final_value=old.final_value,
                reason=None,
                membership_digest=old.membership_digest,
                execution_result="PENDING",
                capability=dict(old.capability),
                parent_ref=dict(old.parent_ref) if old.parent_ref else None,
            )
        )
    enqueue_after_commit(
        session,
        context=context,
        task_name=MANAGEMENT_DISPATCH_TASK,
        task_key=f"ad-management:{task.id}",
        payload={"task_id": str(task.id), "generation": 1},
    )
    session.flush()
    return _public(task)


def prepare_restore(
    session: Session, context: TenantContext, task_id: UUID
) -> ManagementPreviewPublic:
    task = session.exec(
        select(ManagementTask).where(
            ManagementTask.id == task_id,
            ManagementTask.tenant_id == context.tenant_id,
        )
    ).one_or_none()
    if task is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    _scoped_task(session, context, task.bc_id, task.id, action="ads_manage")
    old_items = session.exec(
        select(ManagementTaskItem)
        .where(
            ManagementTaskItem.task_id == task.id,
            ManagementTaskItem.tenant_id == context.tenant_id,
        )
        .order_by(col(ManagementTaskItem.position))
    ).all()
    source_preview = session.get(ManagementPreview, task.preview_id)
    if source_preview is None:
        raise DomainError("management_preview_not_found", "原管理预览不存在")
    now = datetime.now(UTC)
    route = _route(task)
    digest_payload = {"task": str(task.id), "route": route.model_dump(mode="json"), "restore": True}
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True).encode()).hexdigest()
    preview = ManagementPreview(
        tenant_id=task.tenant_id,
        bc_id=task.bc_id,
        actor_id=context.actor_id,
        selection_id=source_preview.selection_id,
        digest=digest,
        mutation={"field": "restore", "source_task_id": str(task.id)},
        route=route.model_dump(mode="json"),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        status="READY",
        counts={"selected": len(old_items), "targets": len(old_items), "linked": 0, "unsupported": 0},
    )
    session.add(preview)
    session.flush()
    public_items: list[ManagementItemPublic] = []
    for position, old in enumerate(old_items):
        conflict = False
        already_disabled = False
        ref = old.ref
        current = session.get(
            AdObject,
            (
                old.tenant_id,
                str(ref.get("advertiser_id")),
                str(ref.get("kind")),
                str(ref.get("remote_id")),
            ),
        )
        operation = str((old.capability or {}).get("operation"))
        if current is None:
            conflict = True
        elif operation == "set_status":
            status = current.operation_status
            if status is None:
                conflict = True
            elif status == "DISABLE":
                already_disabled = True
            elif str(status) != str(old.final_value):
                conflict = True
        else:
            key = "roas_bid" if operation == "update_roas" else "budget"
            value = (current.configuration or {}).get(key)
            if value is None or str(value) != str(old.final_value):
                conflict = True
        reason = "configuration_conflict" if conflict else ("already_disabled" if already_disabled else None)
        execution_result = "CONFLICT" if conflict else ("NO_CHANGE" if already_disabled else "PENDING")
        session.add(
            ManagementPreviewItem(
                tenant_id=old.tenant_id,
                preview_id=preview.id,
                position=position,
                ref=dict(old.ref),
                original_value=old.final_value,
                final_value=old.original_value,
                reason=reason,
                execution_result=execution_result,
                parent_ref=dict(old.parent_ref) if old.parent_ref else None,
                capability=dict(old.capability),
            )
        )
        public_items.append(
            ManagementItemPublic(
                ref=old.ref,
                original_value=None,
                final_value=old.original_value,
                reason=reason,
                execution_result=cast(Any, execution_result),
            )
        )
    session.flush()
    return ManagementPreviewPublic(
        preview_id=preview.id,
        digest=digest,
        created_at=now,
        expires_at=preview.expires_at,
        bc_id=task.bc_id,
        route=route,
        items=tuple(public_items),
        counts=ManagementCounts.model_validate(preview.counts),
    )


def reconcile(
    session: Session, context: TenantContext, task_id: UUID
) -> ManagementTaskPublic:
    task = session.exec(
        select(ManagementTask).where(
            ManagementTask.id == task_id,
            ManagementTask.tenant_id == context.tenant_id,
        )
    ).one_or_none()
    if task is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    _scoped_task(session, context, task.bc_id, task.id, action="read")
    rows = session.exec(
        select(ManagementTaskItem).where(
            ManagementTaskItem.task_id == task.id,
            ManagementTaskItem.tenant_id == context.tenant_id,
        )
    ).all()
    # Reconciliation is deliberately local-only; UNKNOWN and ACCEPTED remain
    # terminal and no provider transport is opened.
    for row in rows:
        if row.execution_result == "UNKNOWN" and task.actor_id == context.actor_id:
            reconcile_item(session, context, row.id)
    return _public(task)


def retry_targeted_refresh(
    session: Session, context: TenantContext, task_id: UUID
) -> ManagementTaskPublic:
    """Queue only durable post-write refreshes; never replay a mutation."""
    task = session.exec(
        select(ManagementTask)
        .where(
            ManagementTask.id == task_id,
            ManagementTask.tenant_id == context.tenant_id,
        )
        .with_for_update()
    ).one_or_none()
    if task is None:
        raise DomainError("management_task_not_found", "管理任务不存在")
    _scoped_task(session, context, task.bc_id, task.id, action="ads_manage")
    pending = session.exec(
        select(ManagementTaskItem.id).where(
            ManagementTaskItem.task_id == task.id,
            ManagementTaskItem.tenant_id == context.tenant_id,
            ManagementTaskItem.execution_result == "ACCEPTED",
            ManagementTaskItem.observation_state == "REFRESH_PENDING",
        )
    ).all()
    if not pending:
        raise DomainError(
            "management_refresh_retry_not_allowed", "没有待重试的定向刷新"
        )
    enqueue_after_commit(
        session,
        context=context,
        task_name=MANAGEMENT_DISPATCH_TASK,
        task_key=f"ad_management.refresh.manual:{task.id}:{uuid4()}",
        payload={"task_id": str(task.id), "generation": 1},
    )
    task.status = "RUNNING"
    session.add(task)
    session.flush()
    return _public(task)


__all__ = [
    "cancel_task",
    "get_task",
    "list_tasks",
    "prepare_restore",
    "reconcile",
    "retry_targeted_refresh",
    "retry_task",
]
