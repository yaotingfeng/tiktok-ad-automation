from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.models import User
from app.modules.accounts.models import BCAccountAccess, TenantBC
from app.modules.ads.directory import append_campaign_name_projection
from app.modules.ads.models import AdObject
from app.modules.reporting.query_models import QuerySnapshot
from app.modules.reporting.schemas import ReportingFilter, SelectionRequest
from app.modules.tenants.models import TenantMembership


@pytest.fixture
def session():
    """服务接收已有事务时必须具有一致读；生产 Engine 会自行开启短事务。"""
    from app.core.db import engine, init_db

    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        transaction = connection.begin()
        try:
            with Session(bind=connection, join_transaction_mode="create_savepoint") as value:
                init_db(value)
                yield value
        finally:
            transaction.rollback()


def filters_for(dimension="campaign", **changes):
    return ReportingFilter(
        dimension=dimension, start_date=date(2026, 9, 30), end_date=date(2026, 9, 30), **changes
    )


def query(case, **kwargs):
    from app.modules.reporting.queries import query_ads

    return query_ads(case.session, context=case.context, bc_id=case.bc_id, **kwargs)


def freeze(case, snapshot_id, **kwargs):
    from app.modules.reporting.selection import freeze_selection

    return freeze_selection(
        case.session, context=case.context, bc_id=case.bc_id,
        request=SelectionRequest(snapshot_id=snapshot_id, mode="ALL_MATCHING", **kwargs),
    )


def test_all_matching_freezes_snapshot(report_case):
    case = report_case
    for index in range(51):
        case.seed_campaign(f"Provider-{index:03d}-Drama", "report-account", 1, 2)
    case.seed_campaign("Other-Drama", "report-account", 1, 2)
    filters = filters_for(query="Provider", sort_by="spend", sort_direction="desc")
    first = query(case, filters=filters, limit=50)
    second = query(case, filters=filters, snapshot_id=first.snapshot.snapshot_id,
                   cursor=first.next_cursor, limit=50)
    assert (first.total, len(first.items), len(second.items)) == (51, 50, 1)
    assert second.next_cursor is None
    assert len({row.row_key for row in first.items + second.items}) == 51
    assert first.summary["buckets"][0]["values"]["spend"] == "51"
    new_ref = case.seed_campaign("Provider-new-Drama", "report-account", 1, 2)
    selected = freeze(case, first.snapshot.snapshot_id)
    assert len(selected.refs) == 51
    assert new_ref.remote_id not in {ref.remote_id for ref in selected.refs}
    reread = query(case, filters=filters, snapshot_id=first.snapshot.snapshot_id, limit=100)
    assert reread.total == first.total
    assert reread.summary == first.summary
    assert reread.items == first.items + second.items


def test_account_selection_captures_campaigns_at_query_time(report_case):
    case = report_case
    old = case.seed_campaign("P-Old", "report-account", 1, 2)
    page = query(case, filters=filters_for("account"))
    case.seed_campaign("P-New", "report-account", 1, 2)
    assert freeze(case, page.snapshot.snapshot_id).refs == (old,)
    assert page.items[0].refs == (old,)


def test_snapshot_preserves_coverage_and_versions(report_case):
    case = report_case
    ref = case.seed_campaign("P-Old", "report-account", 1, 2)
    append_campaign_name_projection(case.session, campaign_ref=ref, raw_name="P-Old", parser_revision=1)
    page = query(case, filters=filters_for())
    assert page.coverage["status"] != "COMPLETE"
    assert page.items[0].coverage["status"] == "MISSING"
    snapshot = case.session.get(QuerySnapshot, page.snapshot.snapshot_id)
    assert snapshot.publication_versions
    assert snapshot.naming_versions


def test_rename_keeps_old_snapshot_and_new_query_regroups(report_case):
    case = report_case
    ref = case.seed_campaign("P-Old", "report-account", 1, 2)
    append_campaign_name_projection(case.session, campaign_ref=ref, raw_name="P-Old", parser_revision=1)
    filters = filters_for("drama")
    old = query(case, filters=filters)
    obj = case.session.get(AdObject, (ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id))
    obj.name = "P-New"
    case.session.add(obj)
    append_campaign_name_projection(case.session, campaign_ref=ref, raw_name="P-New", parser_revision=1)
    case.session.flush()
    reread = query(case, filters=filters, snapshot_id=old.snapshot.snapshot_id)
    new = query(case, filters=filters)
    assert reread.items == old.items
    assert old.items[0].display["name"] == "Old"
    assert new.items[0].display["name"] == "New"
    assert freeze(case, old.snapshot.snapshot_id).refs == (ref,)


@pytest.mark.parametrize("limit", [0, 101])
def test_query_limit_is_bounded(report_case, limit):
    with pytest.raises(HTTPException) as exc:
        query(report_case, filters=filters_for(), limit=limit)
    assert exc.value.status_code == 422


def test_snapshot_owner_scope_and_expiry(report_case):
    from app.modules.reporting.queries import query_ads, snapshot_trend
    from app.modules.reporting.selection import freeze_selection

    case = report_case
    case.seed_campaign("P-A", "report-account", 1, 2)
    case.seed_campaign("P-B", "report-account", 1, 2)
    filters = filters_for()
    page = query(case, filters=filters, limit=1)
    actor = User(username=f"viewer-{uuid4().hex}", hashed_password="test", is_active=True)
    case.session.add(actor)
    case.session.flush()
    case.session.add(TenantMembership(tenant_id=case.context.tenant_id, user_id=actor.id, role="viewer", active=True))
    case.session.flush()
    viewer = TenantContext(case.context.tenant_id, actor.id, "viewer")
    other_bc = TenantBC(tenant_id=case.context.tenant_id, bc_id="other-bc")
    case.session.add(other_bc)
    case.session.flush()
    for request_context, request_bc in ((viewer, case.bc_id), (case.other_context, "bc-other"), (case.context, "other-bc")):
        def calls(context=request_context, bc_id=request_bc):
            return (
                lambda: query_ads(case.session, context=context, bc_id=bc_id, filters=filters,
                                  snapshot_id=page.snapshot.snapshot_id, cursor=page.next_cursor),
                lambda: snapshot_trend(case.session, context=context, bc_id=bc_id,
                                       snapshot_id=page.snapshot.snapshot_id, filters=filters),
                lambda: freeze_selection(case.session, context=context, bc_id=bc_id,
                                         request=SelectionRequest(snapshot_id=page.snapshot.snapshot_id, mode="ALL_MATCHING")),
            )
        for call in calls():
            with pytest.raises(HTTPException) as exc:
                call()
            assert exc.value.status_code == 404
    snapshot = case.session.get(QuerySnapshot, page.snapshot.snapshot_id)
    snapshot.created_at = datetime.now(UTC) - timedelta(minutes=16)
    snapshot.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    case.session.add(snapshot)
    case.session.flush()
    with pytest.raises(HTTPException) as exc:
        query(case, filters=filters, snapshot_id=snapshot.id)
    assert (exc.value.status_code, exc.value.detail) == (409, "query_snapshot_expired")


def test_cursor_tampering_and_filter_mismatch(report_case):
    import base64
    import json

    case = report_case
    for name in ("P-A", "P-B"):
        case.seed_campaign(name, "report-account", 1, 2)
    filters = filters_for()
    page = query(case, filters=filters, limit=1)
    for cursor in ("invalid", page.next_cursor[:-2] + "xx"):
        with pytest.raises(HTTPException) as exc:
            query(case, filters=filters, snapshot_id=page.snapshot.snapshot_id, cursor=cursor)
        assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        query(case, filters=filters_for(query="A"), snapshot_id=page.snapshot.snapshot_id, cursor=page.next_cursor)
    # Even if the attacker knows the payload format, editing actor/BC/sequence
    # cannot produce a valid continuation token.
    payload = {"snapshot_id": str(page.snapshot.snapshot_id), "sequence": 42}
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(HTTPException) as exc:
        query(case, filters=filters, snapshot_id=page.snapshot.snapshot_id, cursor=forged)
    assert exc.value.status_code == 404


def test_current_account_revocation_rejects_snapshot_and_selection(report_case):
    from app.modules.reporting.selection import get_frozen_selection

    case = report_case
    case.seed_campaign("P-A", "report-account", 1, 2)
    page = query(case, filters=filters_for())
    selected = freeze(case, page.snapshot.snapshot_id)
    grant = case.session.exec(select(BCAccountAccess).where(BCAccountAccess.tenant_id == case.context.tenant_id)).first()
    grant.authorized = False
    case.session.add(grant)
    case.session.flush()
    with pytest.raises(HTTPException) as exc:
        query(case, filters=filters_for(), snapshot_id=page.snapshot.snapshot_id)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        get_frozen_selection(case.session, context=case.context, bc_id=case.bc_id, selection_id=selected.selection_id)
    assert exc.value.status_code == 403
