"""广告管理权限直接由账户同步时已核实的搭建/上传权限推导，不再依赖单独的能力记录。"""

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.accounts.access import usable_grants
from app.modules.accounts.connection_models import ConnectionAuthorization
from app.modules.accounts.models import BCAccountAccess
from app.modules.accounts.routing import verify_route


def _verify(session, env, operation, kind):
    context, _, account, route, _ = env
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=account.advertiser_id,
        capability="ads_manage",
        operation=operation,
        entity_kind=kind,
    )


def _grant(session, env):
    context, bc, account, route, _ = env
    return session.get(
        BCAccountAccess,
        (context.tenant_id, bc.bc_id, account.advertiser_id, route.connection_id),
    )


def _scopes(session, env, scopes):
    context, _, _, route, _ = env
    authorization = session.exec(
        select(ConnectionAuthorization).where(
            ConnectionAuthorization.connection_id == route.connection_id
        )
    ).one()
    authorization.scopes = scopes
    session.flush()


def test_build_permission_allows_status_budget_roas(session, management_env):
    for operation, kind in (
        ("set_status", "campaign"),
        ("set_status", "adgroup"),
        ("set_status", "ad"),
        ("update_budget", "campaign"),
        ("update_budget", "adgroup"),
        ("update_roas", "adgroup"),
    ):
        _verify(session, management_env, operation, kind)


def test_without_build_permission_management_is_denied(session, management_env):
    # 只读角色（ANALYST）或授权缺少广告管理时，账户同步不会给出搭建权限。
    _grant(session, management_env).can_build = False
    session.flush()
    with pytest.raises(DomainError, match="management_permission_unverified"):
        _verify(session, management_env, "set_status", "ad")


def test_material_switch_needs_creative_permission(session, management_env):
    # 官方连接：素材开关还需创意管理 scope（6/61/611）；MCP 连接以可上传为准。
    _scopes(session, management_env, ["2"])
    with pytest.raises(DomainError, match="management_permission_unverified"):
        _verify(session, management_env, "set_material_status", "ad")
    _scopes(session, management_env, ["2", "6"])
    _verify(session, management_env, "set_material_status", "ad")


def test_operation_and_entity_kind_are_still_constrained(session, management_env):
    with pytest.raises(DomainError):
        _verify(session, management_env, "update_roas", "campaign")
    with pytest.raises(DomainError, match="management_permission_unverified"):
        _verify(session, management_env, None, None)


def test_usable_grants_follow_the_same_rule(session, management_env):
    context, bc, account, route, _ = management_env
    statement = usable_grants(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        action="ads_manage",
        operation="set_status",
        entity_kind="ad",
    )
    assert [g.advertiser_id for g in session.exec(statement).all()] == [
        account.advertiser_id
    ]
    _grant(session, management_env).can_build = False
    session.flush()
    assert session.exec(statement).all() == []


def test_usable_grants_material_switch_needs_creative_scope(session, management_env):
    context, bc, account, _, _ = management_env
    statement = usable_grants(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        action="ads_manage",
        operation="set_material_status",
        entity_kind="ad",
    )
    _scopes(session, management_env, ["2"])
    assert session.exec(statement).all() == []
    _scopes(session, management_env, ["2", "611"])
    assert [g.advertiser_id for g in session.exec(statement).all()] == [
        account.advertiser_id
    ]
    _scopes(session, management_env, ["mcp:tt4b"])
    assert len(session.exec(statement).all()) == 1  # MCP：可上传即可开关素材
    _grant(session, management_env).can_upload = False
    session.flush()
    assert session.exec(statement).all() == []
