from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlmodel import Session

from app.core.errors import DomainError
from app.core.local_read_batch import local_read_batch
from app.modules.builds.scene import read_scene_context
from app.modules.builds.scene_jobs import ensure_scene_preparation
from app.modules.providers.models import PromotionLink


def test_same_account_mini_scene_is_shared_but_each_link_is_checked(
    database_engine, scene_case
):
    case = scene_case
    kwargs = {
        "context": case["context"],
        "bc_id": case["route"].bc_id,
        "advertiser_id": case["advertiser_id"],
        "route": case["route"],
    }
    with Session(database_engine) as db, db.begin():
        ensure_scene_preparation(db, link_id=case["link_id"], **kwargs)
        original = db.get(PromotionLink, case["link_id"])
        links = [original]
        for _ in range(9):
            link = PromotionLink(
                **(
                    original.model_dump()
                    | {"id": uuid4(), "reuse_key": uuid4().hex * 2, "is_current": False}
                )
            )
            db.add(link)
            links.append(link)
        db.flush()
        statements = []

        def record(_c, _cu, statement, _p, _ct, _many):
            statements.append(statement)

        conn = db.connection()
        event.listen(conn, "before_cursor_execute", record)
        try:
            with local_read_batch(db):
                results = [
                    read_scene_context(db, link_id=link.id, **kwargs) for link in links
                ]
            assert all(result == results[0] for result in results)
            assert not results[0].supported
            assert (
                len([sql for sql in statements if "FROM build_scene_job" in sql]) <= 2
            )
            # 未核实的新链接不能借另一条链接的已缓存账户场景进入预览。
            links[-1].status = "failed"
            db.flush()
            with local_read_batch(db):
                read_scene_context(db, link_id=links[0].id, **kwargs)
                with pytest.raises(DomainError) as error:
                    read_scene_context(db, link_id=links[-1].id, **kwargs)
                assert error.value.code == "scene_link_unavailable"
        finally:
            event.remove(conn, "before_cursor_execute", record)
        # fixture 自有事务回滚，不将克隆链接或排队事件交给任何 worker。
        db.rollback()


def test_other_transaction_revocation_is_seen_before_batch_checkpoint(
    database_engine, scene_case
):
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.accounts.routing import verify_route

    case = scene_case
    key = (
        case["context"].tenant_id,
        case["route"].bc_id,
        case["advertiser_id"],
        case["route"].connection_id,
    )
    try:
        with Session(database_engine) as db, db.begin():
            with pytest.raises(DomainError) as error:
                with local_read_batch(db):
                    verify_route(
                        db,
                        context=case["context"],
                        route=case["route"],
                        advertiser_id=case["advertiser_id"],
                        capability="read",
                    )
                    with Session(database_engine) as admin, admin.begin():
                        grant = admin.get(BCAccountAccess, key)
                        grant.active = False
                        admin.add(grant)
            assert error.value.code == "account_access_denied"
    finally:
        with Session(database_engine) as admin, admin.begin():
            grant = admin.get(BCAccountAccess, key)
            grant.active = True
            admin.add(grant)


def test_held_mini_target_is_refreshed_during_final_check(database_engine, scene_case):
    from app.modules.builds.mini_targets import MiniTarget, url_key

    case = scene_case
    with Session(database_engine) as db:
        link = db.get(PromotionLink, case["link_id"])
        key = (case["context"].tenant_id, url_key(link.url))
        held = db.get(MiniTarget, key)
        before = held.minis_id
        try:
            with pytest.raises(DomainError):
                with local_read_batch(db):
                    read_scene_context(
                        db,
                        context=case["context"],
                        bc_id=case["route"].bc_id,
                        advertiser_id=case["advertiser_id"],
                        link_id=link.id,
                        route=case["route"],
                    )
                    with Session(database_engine) as admin, admin.begin():
                        row = admin.get(MiniTarget, key)
                        row.minis_id = "changed-mini"
                        admin.add(row)
                assert held.minis_id == before
        finally:
            with Session(database_engine) as admin, admin.begin():
                row = admin.get(MiniTarget, key)
                row.minis_id = before
                admin.add(row)


def test_missing_shared_scene_does_not_repeat_lookup(database_engine, scene_case):
    case = scene_case
    with Session(database_engine) as db:
        conn = db.connection()
        statements = []

        def counted(_c, _cu, sql, _p, _ct, _many):
            statements.append(sql)

        event.listen(conn, "before_cursor_execute", counted)
        try:
            result = read_scene_context(
                db,
                context=case["context"],
                bc_id=case["route"].bc_id,
                advertiser_id=case["advertiser_id"],
                link_id=case["link_id"],
                route=case["route"],
            )
            assert "scene_evidence_missing" in result.reason_codes
            assert (
                len([sql for sql in statements if "FROM build_scene_job" in sql]) == 1
            )
        finally:
            event.remove(conn, "before_cursor_execute", counted)
