"""连接列表的有界白名单投影，不解密凭据或触发外部读取。"""

from datetime import UTC, datetime

from sqlalchemy import case, func
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import ERROR_HTTP_STATUS
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
    McpAuthorizationAttempt,
    McpRefreshAttempt,
)
from app.modules.accounts.models import TikTokConnection
from app.modules.accounts.schemas import ConnectionPublic


def enrich_connections(
    session: Session,
    *,
    context: TenantContext,
    items: list[ConnectionPublic],
    bc_id: str | None,
) -> None:
    if not items:
        return
    ids = [item.id for item in items]
    # 输入来自已分页的连接行；每个查询至多取一条当前授权/最近任务。
    binding_rows = session.exec(
        select(
            BCConnectionBinding.connection_id,
            func.count(),
            func.min(BCConnectionBinding.bc_id),
            func.sum(case((col(BCConnectionBinding.status) == "SYNCING", 1), else_=0)),
        )
        .where(
            BCConnectionBinding.tenant_id == context.tenant_id,
            col(BCConnectionBinding.connection_id).in_(ids),
            BCConnectionBinding.status != "DISABLED",
        )
        .group_by(col(BCConnectionBinding.connection_id))
    ).all()
    bindings = {
        identity: (count, first, pending)
        for identity, count, first, pending in binding_rows
    }
    defaults_query = select(BCDefaultRoute.connection_id).where(
        BCDefaultRoute.tenant_id == context.tenant_id,
        col(BCDefaultRoute.connection_id).in_(ids),
    )
    if bc_id is not None:
        defaults_query = defaults_query.where(BCDefaultRoute.bc_id == bc_id)
    defaults = set(session.exec(defaults_query.distinct()).all())
    facts = {
        row.connection_id: row
        for row in session.exec(
            select(ConnectionAuthorization)
            .join(
                TikTokConnection,
                (col(TikTokConnection.id) == col(ConnectionAuthorization.connection_id))
                & (
                    col(TikTokConnection.tenant_id)
                    == col(ConnectionAuthorization.tenant_id)
                )
                & (
                    col(TikTokConnection.authorization_revision)
                    == col(ConnectionAuthorization.authorization_revision)
                ),
            )
            .where(
                ConnectionAuthorization.tenant_id == context.tenant_id,
                col(ConnectionAuthorization.connection_id).in_(ids),
            )
        ).all()
    }
    attempts = {
        row.connection_id: row
        for row in session.exec(
            select(McpAuthorizationAttempt)
            .where(
                McpAuthorizationAttempt.tenant_id == context.tenant_id,
                col(McpAuthorizationAttempt.connection_id).in_(ids),
            )
            .distinct(col(McpAuthorizationAttempt.connection_id))
            .order_by(
                col(McpAuthorizationAttempt.connection_id),
                col(McpAuthorizationAttempt.created_at).desc(),
                col(McpAuthorizationAttempt.id).desc(),
            )
        ).all()
    }
    refreshes = {
        row.connection_id: row
        for row in session.exec(
            select(McpRefreshAttempt)
            .where(
                McpRefreshAttempt.tenant_id == context.tenant_id,
                col(McpRefreshAttempt.connection_id).in_(ids),
            )
            .distinct(col(McpRefreshAttempt.connection_id))
            .order_by(
                col(McpRefreshAttempt.connection_id),
                col(McpRefreshAttempt.created_at).desc(),
                col(McpRefreshAttempt.id).desc(),
            )
        ).all()
    }
    # 授权在选择 BC 时统一发布，完成时间不再依赖某个 BC 的发现任务。
    accepted_times = dict(
        session.exec(
            select(
                McpAuthorizationAttempt.connection_id,
                func.max(McpAuthorizationAttempt.completed_at),
            )
            .where(
                McpAuthorizationAttempt.tenant_id == context.tenant_id,
                col(McpAuthorizationAttempt.connection_id).in_(ids),
                McpAuthorizationAttempt.status == "ACCEPTED",
            )
            .group_by(col(McpAuthorizationAttempt.connection_id))
        ).all()
    )
    now = datetime.now(UTC)
    for item in items:
        item.binding_count, first, item.pending_binding_count = bindings.get(
            item.id, (0, None, 0)
        )
        item.bound_bc_id = first if item.binding_count == 1 else None
        item.is_default = item.id in defaults
        fact = facts.get(item.id)
        accepted_at = accepted_times.get(item.id)
        if accepted_at is not None:
            item.last_authorized_at = accepted_at
        if fact is not None:
            item.evidence_checked_at = fact.verified_at
            if (
                item.status == "ACTIVE"
                and bool(fact.scopes)
                and fact.verified_at is not None
                and 0
                <= (now - fact.verified_at).total_seconds()
                <= settings.BC_CAPABILITY_MAX_AGE_SECONDS
                and fact.source
                and fact.source != "UNKNOWN"
            ):
                for capability in ("read", "upload", "build"):
                    value = fact.permission_summary.get(f"{capability}_authorized")
                    setattr(
                        item,
                        f"{capability}_authorized",
                        value if type(value) is bool else None,
                    )
        attempt = attempts.get(item.id)
        if attempt is not None:
            if attempt.status == "ACCEPTED" and attempt.completed_at is not None:
                item.last_authorized_at = attempt.completed_at
            item.authorization_status = attempt.status
            # 管理员才获得可继续操作的候选引用；失效候选不会触发浏览器自动读取。
            if (
                context.role in {"tenant_admin", "platform_admin"}
                and attempt.expires_at > now
                and attempt.status == "CANDIDATE_READY"
            ):
                item.authorization_attempt_id = attempt.id
            if attempt.error_code in ERROR_HTTP_STATUS:
                item.error_code = attempt.error_code
        refresh = refreshes.get(item.id)
        # 新授权生效前的刷新失败只属于历史，不能让刚完成重连的用户再次重连。
        if refresh is not None and (
            accepted_at is None or refresh.created_at >= accepted_at
        ):
            item.refresh_status = refresh.status
            if refresh.error_code in ERROR_HTTP_STATUS:
                item.error_code = refresh.error_code
