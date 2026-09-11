"""显式 BC 选择与本地连接停用；候选发现成功前不发布当前执行路由。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.integrations.tiktok.mcp_auth.bootstrap import _observation, candidate_attempt
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    McpAuthorizationAttempt,
)
from app.modules.accounts.models import DiscoveryRun, TikTokConnection
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant

register_dispatch_task("accounts.mcp_discover", "resources")


def bind_candidate_bc(
    session: Session, *, context: TenantContext, attempt_id: UUID, bc_id: str
) -> UUID:
    attempt = candidate_attempt(session, context=context, attempt_id=attempt_id)
    # 所有候选/停用/发布路径以连接行为互斥点；锁后再次读取权限与候选状态。
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.id == attempt.connection_id,
            TikTokConnection.tenant_id == context.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    attempt = candidate_attempt(session, context=context, attempt_id=attempt_id)
    if type(bc_id) is not str or not bc_id.strip() or len(bc_id) > 128:
        raise DomainError("mcp_candidate_bc_unknown", "请选择候选授权可见的 BC")
    binding = session.exec(
        select(BCConnectionBinding).where(
            BCConnectionBinding.tenant_id == context.tenant_id,
            BCConnectionBinding.connection_id == connection.id,
        )
    ).first()
    if binding is not None and binding.bc_id != bc_id:
        raise DomainError("mcp_connection_bc_conflict", "每个 MCP 连接只能绑定一个 BC")
    observation = _observation(session, context=context, attempt_id=attempt_id)
    if (
        observation is None
        or observation.call_evidence.get("business_centers_complete") is not True
    ):
        raise DomainError(
            "mcp_candidate_directory_incomplete", "请先完整读取候选 BC 目录"
        )
    bcs = observation.call_evidence.get("business_centers", [])
    if not any(item.get("bc_id") == bc_id for item in bcs):
        raise DomainError("mcp_candidate_bc_unknown", "请选择候选授权可见的 BC")
    existing = session.exec(
        select(DiscoveryRun)
        .where(
            DiscoveryRun.tenant_id == context.tenant_id,
            DiscoveryRun.connection_id == connection.id,
            DiscoveryRun.mcp_candidate_attempt_id == attempt_id,
        )
        .order_by(col(DiscoveryRun.created_at).desc())
    ).first()
    if existing is not None:
        if existing.work.get("bc_id") != bc_id:
            raise DomainError(
                "mcp_connection_bc_conflict", "候选已明确选择其他 BC，请重新发起授权"
            )
        return existing.id
    running = session.exec(
        select(DiscoveryRun).where(
            DiscoveryRun.tenant_id == context.tenant_id,
            DiscoveryRun.connection_id == connection.id,
            col(DiscoveryRun.status).in_(["RUNNING", "ADMISSION_WAIT"]),
        )
    ).first()
    if running is not None:
        raise DomainError("mcp_discovery_in_progress", "当前连接正在发现，请稍后重试")
    run = DiscoveryRun(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        mcp_candidate_attempt_id=attempt_id,
        credential_revision=attempt.base_credential_revision,
        work={
            "stage": "MCP_VERIFY",
            "bc_id": bc_id,
            "observation_id": str(observation.id),
            "page": 1,
        },
    )
    session.add(run)
    session.flush()
    enqueue_after_commit(
        session,
        context=context,
        task_name="accounts.mcp_discover",
        task_key=f"mcp-candidate:{attempt_id}:discover",
        payload={"run_id": str(run.id)},
    )
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="tiktok.mcp.binding.request",
            target_id=str(connection.id),
            details={
                "attempt_id": str(attempt_id),
                "bc_id": bc_id,
                "run_id": str(run.id),
            },
        )
    )
    # ACTIVE、BCConnectionBinding、BCDefaultRoute 只能由完整发现后的原子发布写入。
    session.flush()
    return run.id


def disable_connection(
    session: Session,
    *,
    context: TenantContext,
    connection_id: UUID,
    task_deadline: datetime,
) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    if task_deadline.tzinfo is None or task_deadline <= datetime.now(UTC):
        raise DomainError("mcp_deadline_exceeded", "操作时限已过")
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.id == connection_id,
            TikTokConnection.tenant_id == context.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if connection is None:
        raise DomainError("connection_not_found", "当前租户连接不存在")
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    if connection.status == "DISABLED":
        return
    connection.status = "DISABLED"
    connection.authorization_revision += 1
    session.add(connection)
    attempts = session.exec(
        select(McpAuthorizationAttempt).where(
            McpAuthorizationAttempt.tenant_id == context.tenant_id,
            McpAuthorizationAttempt.connection_id == connection_id,
            col(McpAuthorizationAttempt.status).in_(
                ["PENDING", "CLAIMED", "CANDIDATE_READY"]
            ),
        )
    ).all()
    attempt_ids = {str(attempt.id) for attempt in attempts}
    for attempt in attempts:
        attempt.status = "CANCELLED"
        attempt.pkce_verifier_ciphertext = None
        attempt.candidate_ciphertext = None
        attempt.completed_at = datetime.now(UTC)
        session.add(attempt)
    runs = session.exec(
        select(DiscoveryRun).where(
            DiscoveryRun.tenant_id == context.tenant_id,
            DiscoveryRun.connection_id == connection_id,
            col(DiscoveryRun.status).in_(["RUNNING", "ADMISSION_WAIT"]),
        )
    ).all()
    run_ids = {str(run.id) for run in runs}
    for run in runs:
        run.status = "CANCELLED"
        run.error_code = "connection_unavailable"
        session.add(run)
    pending = session.exec(
        select(PendingDispatch)
        .where(
            PendingDispatch.tenant_id == context.tenant_id,
            col(PendingDispatch.published_at).is_(None),
        )
        .with_for_update()
    ).all()
    cancelled = 0
    for dispatch in pending:
        payload = dispatch.payload
        if (
            payload.get("connection_id") == str(connection_id)
            or payload.get("attempt_id") in attempt_ids
            or payload.get("run_id") in run_ids
        ):
            session.delete(dispatch)
            cancelled += 1
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="tiktok.connection.disable",
            target_id=str(connection_id),
            details={"cancelled_dispatches": cancelled},
        )
    )
    # 已发送 attempt 与回执保留；本地停用不对上游 grant 发起隐式撤销。
    session.flush()


def request_mcp_revocation(
    session: Session, *, context: TenantContext, connection_id: UUID
) -> UUID:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    connection = session.get(TikTokConnection, connection_id, populate_existing=True)
    if (
        connection is None
        or connection.tenant_id != context.tenant_id
        or connection.kind != "OFFICIAL_MCP"
    ):
        raise DomainError("connection_not_found", "当前租户 MCP 连接不存在")
    load_mcp_protocol().require_authorization_verified()
    # P0 只确认 revoke URL，没有 grant 隔离与撤销范围证明。不能把端点存在当作安全撤销授权。
    raise DomainError(
        "mcp_revocation_unsupported", "官方 MCP 撤销隔离语义尚未核实，请使用本地停用"
    )
