from typing import cast
from uuid import UUID

from sqlalchemy import String, and_, func, or_
from sqlalchemy import cast as sql_cast
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.models import User
from app.modules.tenants.models import AuditEvent, Tenant, TenantMembership
from app.modules.tenants.permissions import Role, require_tenant
from app.modules.tenants.schemas import (
    MemberPublic,
    MemberRole,
    TenantSummary,
    UserCandidate,
)


def require_platform(session: Session, *, actor_id: UUID) -> User:
    actor = session.get(User, actor_id, populate_existing=True)
    if not actor or not actor.is_active or not actor.is_superuser:
        raise DomainError("platform_forbidden", "仅平台管理员可管理租户")
    return actor


def _valid_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 120:
        raise DomainError("invalid_tenant", "租户名称必须为1至120个字符")
    return name


def create_tenant(
    session: Session, *, actor_id: UUID, name: str, administrator_id: UUID
) -> Tenant:
    require_platform(session, actor_id=actor_id)
    admin = session.get(User, administrator_id, populate_existing=True)
    if not admin or not admin.is_active:
        raise DomainError("invalid_tenant", "初始管理员必须是有效用户")
    tenant = Tenant(name=_valid_name(name))
    session.add(tenant)
    session.flush()
    session.add(
        TenantMembership(
            tenant_id=tenant.id, user_id=administrator_id, role="tenant_admin"
        )
    )
    session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_id=actor_id,
            action="tenant.create",
            target_id=str(tenant.id),
            details={"administrator_id": str(administrator_id)},
        )
    )
    session.flush()
    return tenant


def update_tenant(
    session: Session,
    *,
    actor_id: UUID,
    tenant_id: UUID,
    changes: dict[str, str | bool | None],
) -> Tenant:
    require_platform(session, actor_id=actor_id)
    if (
        not changes
        or set(changes) - {"name", "active"}
        or any(value is None for value in changes.values())
    ):
        raise DomainError("invalid_tenant", "请提供有效的租户修改字段")
    tenant = session.exec(
        select(Tenant)
        .where(Tenant.id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if not tenant:
        raise DomainError("tenant_not_found", "租户不存在")
    # Waiting for a concurrent tenant change must not retain an earlier grant.
    require_platform(session, actor_id=actor_id)
    details: dict[str, str | bool] = {}
    if "name" in changes:
        name = changes["name"]
        if not isinstance(name, str):
            raise DomainError("invalid_tenant", "租户名称无效")
        tenant.name = _valid_name(name)
        details["name"] = tenant.name
    if "active" in changes:
        active = changes["active"]
        if not isinstance(active, bool):
            raise DomainError("invalid_tenant", "租户状态无效")
        tenant.active = active
        details["active"] = active
    session.add(tenant)
    session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_id=actor_id,
            action="tenant.update",
            target_id=str(tenant.id),
            details=details,
        )
    )
    session.flush()
    return tenant


def set_member(
    session: Session, *, context: TenantContext, user_id: UUID, role: Role, active: bool
) -> TenantMembership:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    # Every membership writer serializes on one tenant row, including different
    # members. Row locks on the member being changed would allow write skew.
    tenant = session.exec(
        select(Tenant)
        .where(Tenant.id == context.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    context = require_tenant(
        session, actor_id=context.actor_id, tenant_id=tenant.id, action="manage"
    )
    user = session.get(User, user_id, populate_existing=True)
    if (
        role not in {"tenant_admin", "operator", "viewer"}
        or not user
        or not user.is_active
    ):
        raise DomainError("invalid_member", "成员和租户角色必须有效")
    member = session.get(TenantMembership, (tenant.id, user_id), populate_existing=True)
    if (
        member
        and member.active
        and member.role == "tenant_admin"
        and (not active or role != "tenant_admin")
    ):
        admins = session.exec(
            select(func.count())
            .select_from(TenantMembership)
            .join(User, col(User.id) == TenantMembership.user_id)
            .where(
                TenantMembership.tenant_id == tenant.id,
                col(TenantMembership.active).is_(True),
                TenantMembership.role == "tenant_admin",
                col(User.is_active).is_(True),
            )
        ).one()
        if admins <= 1:
            raise DomainError("last_tenant_admin", "先设置其他租户管理员")
    member = member or TenantMembership(tenant_id=tenant.id, user_id=user_id, role=role)
    member.role, member.active = role, active
    session.add(member)
    session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_id=context.actor_id,
            action="member.set",
            target_id=str(user_id),
            details={"role": role, "active": active},
        )
    )
    session.flush()
    return member


def _page_limit(limit: int) -> None:
    if not 1 <= limit <= 200:
        raise DomainError("configuration_invalid", "每页数量必须为1至200")


def list_tenants(
    session: Session,
    *,
    actor_id: UUID,
    platform_only: bool = False,
    after_id: UUID | None = None,
    limit: int = 50,
    search: str = "",
    active: bool | None = None,
) -> Page[TenantSummary]:
    _page_limit(limit)
    actor = session.get(User, actor_id, populate_existing=True)
    if not actor or not actor.is_active:
        raise DomainError("tenant_forbidden", "用户不可用")
    if platform_only:
        require_platform(session, actor_id=actor_id)
    statement = select(Tenant, TenantMembership.role).outerjoin(
        TenantMembership,
        and_(
            col(TenantMembership.tenant_id) == Tenant.id,
            col(TenantMembership.user_id) == actor_id,
        ),
    )
    if not actor.is_superuser:
        statement = statement.where(
            col(Tenant.active).is_(True), col(TenantMembership.active).is_(True)
        )
    if active is not None:
        statement = statement.where(Tenant.active == active)
    if after_id is not None:
        statement = statement.where(Tenant.id > after_id)
    if search.strip():
        term = search.strip()
        statement = statement.where(
            or_(
                col(Tenant.name).icontains(term, autoescape=True),
                sql_cast(Tenant.id, String) == term,
            )
        )
    rows = session.exec(
        statement.order_by(col(Tenant.id))
        .limit(limit + 1)
        .execution_options(populate_existing=True)
    ).all()
    items = [
        TenantSummary(
            id=tenant.id,
            name=tenant.name,
            active=tenant.active,
            role="platform_admin" if actor.is_superuser else cast(Role, role),
        )
        for tenant, role in rows[:limit]
    ]
    return Page(
        items=items, next_cursor=str(items[-1].id) if len(rows) > limit else None
    )


def member_public(member: TenantMembership, user: User) -> MemberPublic:
    return MemberPublic(
        tenant_id=member.tenant_id,
        user_id=member.user_id,
        role=cast(MemberRole, member.role),
        active=member.active,
        email=user.email,
        full_name=user.full_name,
        user_active=user.is_active,
    )


def list_members(
    session: Session,
    *,
    context: TenantContext,
    after_id: UUID | None = None,
    limit: int = 50,
    search: str = "",
    role: MemberRole | None = None,
    active: bool | None = None,
) -> Page[MemberPublic]:
    _page_limit(limit)
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    statement = (
        select(TenantMembership, User)
        .join(User, col(User.id) == TenantMembership.user_id)
        .where(TenantMembership.tenant_id == context.tenant_id)
    )
    if after_id is not None:
        statement = statement.where(TenantMembership.user_id > after_id)
    if active is not None:
        statement = statement.where(TenantMembership.active == active)
    if role is not None:
        statement = statement.where(TenantMembership.role == role)
    if search.strip():
        term = search.strip()
        statement = statement.where(
            or_(
                col(User.email).icontains(term, autoescape=True),
                col(User.full_name).icontains(term, autoescape=True),
                sql_cast(User.id, String) == term,
            )
        )
    rows = session.exec(
        statement.order_by(col(TenantMembership.user_id))
        .limit(limit + 1)
        .execution_options(populate_existing=True)
    ).all()
    items = [member_public(member, user) for member, user in rows[:limit]]
    return Page(
        items=items, next_cursor=str(items[-1].user_id) if len(rows) > limit else None
    )


def search_user_candidates(
    session: Session,
    *,
    actor_id: UUID,
    query: str,
    tenant_id: UUID | None = None,
    after_id: UUID | None = None,
    limit: int = 50,
) -> Page[UserCandidate]:
    if tenant_id is None:
        require_platform(session, actor_id=actor_id)
    else:
        require_tenant(session, actor_id=actor_id, tenant_id=tenant_id, action="manage")
    _page_limit(limit)
    term = query.strip()
    if not term or len(term) > 255:
        raise DomainError("invalid_member", "请输入用户姓名或邮箱")
    statement = select(User).where(
        col(User.is_active).is_(True),
        or_(
            col(User.email).icontains(term, autoescape=True),
            col(User.full_name).icontains(term, autoescape=True),
        ),
    )
    if after_id is not None:
        statement = statement.where(User.id > after_id)
    rows = session.exec(statement.order_by(col(User.id)).limit(limit + 1)).all()
    items = [
        UserCandidate(id=user.id, email=user.email, full_name=user.full_name)
        for user in rows[:limit]
    ]
    return Page(
        items=items, next_cursor=str(items[-1].id) if len(rows) > limit else None
    )
