"""真实 PostgreSQL：重复权限查询收敛，批次退出和下一轮仍识别撤权。"""

import pytest
from sqlalchemy import event

from app.core.errors import DomainError
from app.modules.accounts.routing import freeze_route, verify_route
from tests.modules.accounts.test_routing import route_case as route_case


def test_repeated_route_checks_are_bounded_and_rechecked_on_exit(session, route_case):
    from app.core.local_read_batch import local_read_batch

    context, connection, grant, _ = route_case
    queries = []

    def counted(*_args):
        queries.append(1)

    db = session.connection()
    event.listen(db, "before_cursor_execute", counted)
    try:
        with local_read_batch(session):
            for _ in range(30):
                route = freeze_route(
                    session,
                    context=context,
                    bc_id=grant.bc_id,
                    connection_id=connection.id,
                )
                verify_route(
                    session,
                    context=context,
                    route=route,
                    advertiser_id=grant.advertiser_id,
                    capability="build",
                )
        assert len(queries) <= 35
    finally:
        event.remove(db, "before_cursor_execute", counted)
    grant.can_build = False
    session.flush()
    with pytest.raises(DomainError, match="角色或范围"):
        with local_read_batch(session):
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=grant.advertiser_id,
                capability="build",
            )


@pytest.mark.parametrize("change", ["grant", "member", "binding", "channel"])
def test_revocation_during_batch_rejected_before_checkpoint(
    session, route_case, change
):
    from app.core.local_read_batch import local_read_batch
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.tenants.models import TenantMembership

    context, connection, grant, _ = route_case
    with pytest.raises(DomainError):
        with local_read_batch(session):
            route = freeze_route(
                session, context=context, bc_id=grant.bc_id, connection_id=connection.id
            )
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=grant.advertiser_id,
                capability="build",
            )
            if change == "grant":
                grant.can_build = False
            elif change == "member":
                session.get(
                    TenantMembership, (context.tenant_id, context.actor_id)
                ).active = False
            elif change == "binding":
                session.get(
                    BCConnectionBinding, (context.tenant_id, grant.bc_id, connection.id)
                ).revision += 1
            else:
                connection.status = "DISABLED"
            session.flush()


def test_cache_does_not_hide_other_capability_or_account(session, route_case):
    from app.core.local_read_batch import local_read_batch

    context, connection, grant, _ = route_case
    grant.can_upload = False
    session.flush()
    with local_read_batch(session):
        route = freeze_route(
            session, context=context, bc_id=grant.bc_id, connection_id=connection.id
        )
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=grant.advertiser_id,
            capability="build",
        )
        for advertiser, capability in [
            (grant.advertiser_id, "upload"),
            ("missing", "build"),
        ]:
            with pytest.raises(DomainError):
                verify_route(
                    session,
                    context=context,
                    route=route,
                    advertiser_id=advertiser,
                    capability=capability,
                )


def test_expired_or_aborted_batch_does_not_reuse_permission(
    session, route_case, monkeypatch
):
    import app.core.local_read_batch as reads

    context, connection, grant, _ = route_case
    route = freeze_route(
        session, context=context, bc_id=grant.bc_id, connection_id=connection.id
    )
    tick = [0.0]
    # 替换时钟，不等待真实秒数；权限检查和数据库仍是真实实现。
    monkeypatch.setattr(reads, "monotonic", lambda: tick[0])
    with pytest.raises(DomainError):
        with reads.local_read_batch(session):
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=grant.advertiser_id,
                capability="build",
            )
            grant.can_build = False
            session.flush()
            tick[0] = 4.0
            verify_route(
                session,
                context=context,
                route=route,
                advertiser_id=grant.advertiser_id,
                capability="build",
            )
    with pytest.raises(DomainError):
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=grant.advertiser_id,
            capability="build",
        )


def test_primary_selection_is_shared_only_for_read_only_batches(session, route_case):
    from app.core.local_read_batch import local_read_batch
    from app.modules.materials.source_selection import read_primary_advertiser

    context, connection, grant, _ = route_case
    route = freeze_route(
        session, context=context, bc_id=grant.bc_id, connection_id=connection.id
    )
    statements = []

    def counted(_c, _cu, statement, _p, _ct, _many):
        statements.append(statement)

    db = session.connection()
    event.listen(db, "before_cursor_execute", counted)
    try:
        with local_read_batch(session):
            for _ in range(20):
                result = read_primary_advertiser(
                    session, context=context, bc_id=grant.bc_id, route=route
                )
                assert result == grant.advertiser_id
        assert (
            len(
                [
                    sql
                    for sql in statements
                    if sql.startswith("SELECT max(source_account_load.cooldown_until)")
                ]
            )
            <= 2
        )
    finally:
        event.remove(db, "before_cursor_execute", counted)
