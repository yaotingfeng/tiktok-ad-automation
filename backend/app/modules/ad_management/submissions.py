"""Atomic management-task submission.

Submission copies the complete preview into task rows and creates one
transactional outbox record.  Workers therefore never need to consult the
short-lived selection or preview to discover their targets, and an outbox
retry cannot silently pick a new route or a newly-created ad.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.jobs.outbox import enqueue_after_commit
from app.modules.accounts.routing import verify_route
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementPreviewItem,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.schemas import ManagementCounts, ManagementTaskPublic
from app.modules.ad_management.tasks import (
    MANAGEMENT_DISPATCH_TASK,
    execute_management_task,  # noqa: F401
)
from app.modules.tenants.permissions import require_tenant


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    # PostgreSQL returns aware values; this also keeps old SQLite fixtures
    # deterministic while still treating them as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _counts(value: dict[str, Any]) -> ManagementCounts:
    return ManagementCounts.model_validate(value or {})


def _public(row: ManagementTask) -> ManagementTaskPublic:
    return ManagementTaskPublic(
        task_id=row.id,
        bc_id=row.bc_id,
        status=cast(Any, row.status),
        counts=_counts(row.counts),
    )


def _idempotency_lock(session: Session, context: TenantContext, key: UUID) -> None:
    """Serialize one tenant/actor key before the unique insert race."""
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"ad-management-submit:{context.tenant_id}:{context.actor_id}:{key}"},
    )


def _check_preview(
    session: Session,
    context: TenantContext,
    preview_id: UUID,
    preview_digest: str,
) -> tuple[ManagementPreview, FrozenTikTokRoute, list[ManagementPreviewItem]]:
    preview = session.exec(
        select(ManagementPreview)
        .where(
            ManagementPreview.id == preview_id,
            ManagementPreview.tenant_id == context.tenant_id,
            ManagementPreview.actor_id == context.actor_id,
        )
        .with_for_update()
    ).one_or_none()
    if preview is None:
        raise DomainError("preview_not_found", "管理预览不存在")
    if preview.digest != preview_digest:
        raise DomainError("preview_digest_conflict", "预览摘要已变化")
    now = _now()
    if _aware(preview.expires_at) <= now:
        preview.status = "EXPIRED"
        raise DomainError(
            "preview_expired", "preview_expired: 管理预览已过期，请重新准备"
        )
    if preview.status != "READY":
        raise DomainError("preview_not_submittable", "管理预览当前不可提交")
    route = FrozenTikTokRoute.model_validate(preview.route)
    if route.tenant_id != context.tenant_id or route.bc_id != preview.bc_id:
        raise DomainError("frozen_route_scope_mismatch", "冻结路由与预览范围不一致")
    # Route generations and tenant/BC access are rechecked while the short
    # database transaction is open.  No provider request happens here.
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=None,
        capability="read",
    )
    operation = {
        "roas": "update_roas",
        "budget": "update_budget",
        "status": "set_status",
        "material_status": "set_material_status",
    }.get(str(preview.mutation.get("field", "")))
    is_restore = str(preview.mutation.get("field", "")) == "restore"
    if operation is None and not is_restore:
        raise DomainError("management_operation_invalid", "冻结管理操作无效")
    items = session.exec(
        select(ManagementPreviewItem)
        .where(
            ManagementPreviewItem.tenant_id == context.tenant_id,
            ManagementPreviewItem.preview_id == preview.id,
        )
        .order_by(col(ManagementPreviewItem.position))
    ).all()
    frozen_route_json = route.model_dump(mode="json")
    for item in items:
        if item.execution_result == "PENDING":
            item_route = item.capability.get("route") if item.capability else None
            if item_route is not None and item_route != frozen_route_json:
                raise DomainError(
                    "frozen_route_scope_mismatch", "管理目标的冻结路由已变化"
                )
            item_operation = item.capability.get("operation") if item.capability else None
            expected_operation = item_operation if is_restore else operation
            if expected_operation not in {
                "update_roas",
                "update_budget",
                "set_status",
                "set_material_status",
            }:
                raise DomainError(
                    "management_operation_invalid", "管理目标操作与预览不一致"
                )
            ref = item.ref
            advertiser_id = ref.get("advertiser_id")
            entity_kind = ref.get("kind")
            if not isinstance(advertiser_id, str) or not isinstance(entity_kind, str):
                raise DomainError("management_request_invalid", "冻结目标身份无效")
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=advertiser_id,
                capability="ads_manage",
                operation=expected_operation,
                entity_kind=entity_kind,
            )
    return preview, route, list(items)


def submit_management_task(
    session: Session,
    context: TenantContext,
    preview_id: UUID,
    preview_digest: str,
    idempotency_key: UUID,
) -> ManagementTaskPublic:
    """Copy one immutable preview and enqueue exactly one dispatch message."""
    if not isinstance(idempotency_key, UUID):
        raise ValueError("idempotency key must be UUID")
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="ads_manage",
    )
    _idempotency_lock(session, context, idempotency_key)
    existing = session.exec(
        select(ManagementTask)
        .where(
            ManagementTask.tenant_id == context.tenant_id,
            ManagementTask.actor_id == context.actor_id,
            ManagementTask.idempotency_key == idempotency_key,
        )
        .with_for_update()
    ).one_or_none()
    if existing is not None:
        if existing.digest != preview_digest:
            raise DomainError(
                "idempotency_conflict", "idempotency_conflict: 幂等键已绑定其他预览摘要"
            )
        return _public(existing)

    preview, route, preview_items = _check_preview(
        session, context, preview_id, preview_digest
    )
    # ``ManagementPreviewItem.capability`` includes the exact route and the
    # operation evidence captured by C3.  Do not search or expand objects at
    # submission time; the preview is the user-authorized target set.
    with session.begin_nested():
        task = ManagementTask(
            tenant_id=context.tenant_id,
            bc_id=preview.bc_id,
            actor_id=context.actor_id,
            preview_id=preview.id,
            idempotency_key=idempotency_key,
            digest=preview.digest,
            route=route.model_dump(mode="json"),
            status="QUEUED",
            counts=preview.counts,
        )
        session.add(task)
        session.flush()
        for position, item in enumerate(preview_items):
            session.add(
                ManagementTaskItem(
                    tenant_id=context.tenant_id,
                    task_id=task.id,
                    preview_item_id=item.id,
                    preview_id=preview.id,
                    position=position,
                    ref=dict(item.ref),
                    material_use=dict(item.material_use) if item.material_use else None,
                    original_value=item.original_value,
                    final_value=item.final_value,
                    reason=item.reason,
                    membership_digest=item.membership_digest,
                    execution_result=item.execution_result,
                    observation_state=item.observation_state,
                    delivery_status=item.delivery_status,
                    request_attribution=item.request_attribution,
                    grouping_revision=item.grouping_revision,
                    parent_ref=dict(item.parent_ref) if item.parent_ref else None,
                    capability={
                        **dict(item.capability),
                        "route": route.model_dump(mode="json"),
                        "preview_digest": preview.digest,
                    },
                )
            )
        # The dispatch payload contains only internal IDs.  Frozen route,
        # targets, and values remain in SQL rows, making this message safe to
        # replay.
        enqueue_after_commit(
            session,
            context=context,
            task_name=MANAGEMENT_DISPATCH_TASK,
            task_key=f"ad-management:{task.id}",
            payload={"task_id": str(task.id), "generation": 1},
        )
        task.updated_at = _now()
        session.add(task)
        session.flush()
    return _public(task)


__all__ = ["MANAGEMENT_DISPATCH_TASK", "submit_management_task"]
