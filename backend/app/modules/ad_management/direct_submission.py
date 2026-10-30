"""直接受理冻结意图；HTTP 请求不展开目录或逐项授权。"""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_many_after_commit
from app.modules.accounts.routing import freeze_route
from app.modules.ad_management.models import ManagementTask
from app.modules.ad_management.preparation_dispatch import preparation_watchdog_entry
from app.modules.ad_management.previews import _digest, _selection_record
from app.modules.ad_management.schemas import ManagementCounts, MutationSpec
from app.modules.ad_management.submissions import (
    MANAGEMENT_DISPATCH_TASK,
    _idempotency_lock,
    _public,
)
from app.modules.tenants.permissions import require_tenant


def submit_direct_task(
    session: Session,
    context: TenantContext,
    bc_id: str,
    selection_id: UUID,
    mutation: MutationSpec,
    idempotency_key: UUID,
    row_problems: list[dict[str, str]] | None = None,
):
    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="ads_manage",
    )
    _idempotency_lock(session, context, idempotency_key)
    intent = {
        "bc_id": bc_id,
        "selection_id": str(selection_id),
        "mutation": mutation.model_dump(mode="json"),
        "row_problems": row_problems or [],
    }
    existing = session.exec(
        select(ManagementTask).where(
            ManagementTask.tenant_id == context.tenant_id,
            ManagementTask.actor_id == context.actor_id,
            ManagementTask.idempotency_key == idempotency_key,
        )
    ).one_or_none()
    # 幂等重试优先读取已保存意图，原选择被清理也能返回同一任务。
    if existing is not None:
        if existing.source_kind != "DIRECT" or existing.request.get("intent") != intent:
            raise DomainError("idempotency_conflict", "幂等键已绑定其他管理操作")
        return _public(existing)
    record, _ = _selection_record(session, context, selection_id, bc_id)
    route = freeze_route(session, context=context, bc_id=bc_id)
    evidence = record.model_dump(mode="json")
    request = {
        "version": 1,
        "intent": intent,
        "mutation": intent["mutation"],
        "selection": deepcopy(evidence),
        "submitted_at": datetime.now(UTC).isoformat(),
        "row_problems": row_problems or [],
    }
    task = ManagementTask(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        actor_id=context.actor_id,
        source_kind="DIRECT",
        request=request,
        idempotency_key=idempotency_key,
        digest=_digest({"request": request, "route": route.model_dump(mode="json")}),
        route=route.model_dump(mode="json"),
        status="PREPARING",
        counts=ManagementCounts(
            selected=(len(record.material_uses) or len(record.refs))
            + len(row_problems or []),
            skipped=len(row_problems or []),
            unsupported=len(row_problems or []),
        ).model_dump(),
    )
    session.add(task)
    session.flush()
    # 首次投递与未来恢复消息一起提交，首块开始前进程失联也能恢复。
    enqueue_many_after_commit(
        session,
        context=context,
        entries=[
            (
                MANAGEMENT_DISPATCH_TASK,
                f"ad-management:{task.id}",
                {"task_id": str(task.id), "generation": 1},
                None,
            ),
            preparation_watchdog_entry(task),
        ],
    )
    return _public(task)
