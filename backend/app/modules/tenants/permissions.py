from typing import Literal
from uuid import UUID

from sqlmodel import Session

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.models import User
from app.modules.tenants.models import Tenant, TenantMembership

Action = Literal[
    "read", "manage", "build", "upload", "strategy_write", "provider_write"
]
Role = Literal["platform_admin", "tenant_admin", "operator", "viewer"]
ALL_ACTIONS = frozenset(
    {"read", "manage", "build", "upload", "strategy_write", "provider_write"}
)
PERMISSIONS = {
    "platform_admin": ALL_ACTIONS,
    "tenant_admin": ALL_ACTIONS,
    "operator": ALL_ACTIONS - {"manage"},
    "viewer": frozenset({"read"}),
}


def require_tenant(
    session: Session, *, actor_id: UUID, tenant_id: UUID, action: str
) -> TenantContext:
    """Rebuild current authority from rows, including on background execution."""
    if action not in ALL_ACTIONS:
        raise DomainError("unknown_action", "未定义的业务动作")
    user = session.get(User, actor_id, populate_existing=True)
    tenant = session.get(Tenant, tenant_id, populate_existing=True)
    if not user or not user.is_active or not tenant or not tenant.active:
        raise DomainError("tenant_forbidden", "无法进入该租户")
    member = session.get(
        TenantMembership, (tenant_id, actor_id), populate_existing=True
    )
    if user.is_superuser:
        role = "platform_admin"
    elif member and member.active:
        role = member.role
    else:
        raise DomainError("tenant_forbidden", "无法进入该租户")
    if action not in PERMISSIONS.get(role, frozenset()):
        raise DomainError("action_forbidden", "当前角色不能执行此操作")
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, role=role)
