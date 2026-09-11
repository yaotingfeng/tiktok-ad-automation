"""失效的原连接仍用于历史展示；公开投影不构成执行授权。"""

from uuid import UUID

from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.accounts.models import TikTokConnection
from app.modules.builds.route_schemas import ExecutionRoutePublic
from app.modules.builds.routes import load_preview_route


def execution_route_view(
    session: Session, *, context: TenantContext, preview_id: UUID
) -> ExecutionRoutePublic | None:
    try:
        route = load_preview_route(session, context=context, preview_id=preview_id)
    except DomainError as error:
        if error.code == "legacy_route_unverifiable":
            return None
        raise
    # 这里只读保存的路线及同租户连接名；停用、重新授权或默认切换不改历史展示。
    name = session.exec(
        select(TikTokConnection.display_name).where(
            TikTokConnection.tenant_id == context.tenant_id,
            TikTokConnection.id == route.connection_id,
        )
    ).one_or_none()
    return ExecutionRoutePublic(
        connection_id=route.connection_id,
        connection_name=name or str(route.connection_id),
        channel=route.channel,
        bc_id=route.bc_id,
    )
