from datetime import date
from decimal import Decimal as D

from sqlmodel import select


def test_preview_commit_conflict_and_explicit_gap(
    business_client,
    session,
    business_case,
):
    from app.modules.business.configuration import rate_on
    from app.modules.business.models import BusinessRateSchedule

    c = business_case
    url = f"/api/tenants/{c.context.tenant_id}/business/rates"
    scope = {"kind": "provider", "scope_id": str(c.provider.id)}
    first = {
        **scope,
        "expected_revision": 0,
        "start_date": "2026-10-01",
        "rate": "0.04",
    }
    assert business_client.get(url, params=scope).json() == {
        "revision": 0,
        "periods": [],
    }
    assert business_client.post(f"{url}/preview", json=first).status_code == 200
    assert session.exec(select(BusinessRateSchedule)).first() is None
    assert business_client.post(url, json=first).status_code == 200
    stale = business_client.post(url, json=first)
    assert stale.status_code == 409
    replace = {
        **scope,
        "expected_revision": 1,
        "start_date": "2026-08-01",
        "rate": "0.06",
    }
    assert business_client.post(url, json=replace).json()["periods"] == [
        {"start_date": "2026-08-01", "end_date": None, "rate": "0.06"}
    ]
    end = {
        **scope,
        "expected_revision": 2,
        "action": "end",
        "target_start_date": "2026-08-01",
        "start_date": "2026-08-01",
        "end_date": "2026-09-30",
    }
    assert business_client.post(url, json=end).status_code == 200
    assert rate_on(
        session, c.context.tenant_id, "provider", str(c.provider.id), date(2026, 9, 30)
    ) == D("0.06")
    assert (
        rate_on(
            session,
            c.context.tenant_id,
            "provider",
            str(c.provider.id),
            date(2026, 10, 1),
        )
        is None
    )
    assert (
        business_client.get(f"{url}/history", params={**scope, "limit": 2}).json()[
            "has_more"
        ]
        is True
    )
    assert (
        business_client.get(
            url, params={"kind": "rebate", "scope_id": "not-authorized"}
        ).status_code
        == 404
    )


def test_missing_rates_propagate_to_totals_and_scenarios(session, business_case):
    from app.modules.business.configuration import save_mapping, save_rate
    from app.modules.business.queries import query_dashboard
    from app.modules.business.schemas import DashboardFilter
    from tests.modules.business.test_projection import build, seed_days

    c = business_case
    seed_days(session, c)
    for campaign in ("a", "b"):
        save_mapping(
            session,
            context=c.context,
            bc_id=c.bc_id,
            advertiser_id=c.advertiser_id,
            campaign_id=campaign,
            drama_id=c.drama.id,
        )
    save_rate(
        session,
        context=c.context,
        kind="provider",
        scope_id=str(c.provider.id),
        expected_revision=0,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 1),
        rate=D("0.06"),
    )
    run = build(session, c)[0]
    assert run.inputs["rate_schedules"]
    from app.modules.business.models import BusinessDay

    details = session.exec(
        select(BusinessDay).where(BusinessDay.run_id == run.id, BusinessDay.spend > 0)
    ).all()
    assert {row.stat_date: row.provider_rate for row in details} == {
        date(2026, 11, 1): D("0.06"),
        date(2026, 11, 2): None,
    }
    for dimension in ("campaign", "drama", "provider", "day"):
        for scenario in ("base", "comic", "other", "existing"):
            result = query_dashboard(
                session,
                context=c.context,
                filters=DashboardFilter(
                    bc_id=c.bc_id,
                    start_date=date(2026, 11, 1),
                    end_date=c.day,
                    dimension=dimension,
                    scenario=scenario,
                ),
            )
            assert result.totals["spend"] == D(2000)
            assert result.totals["revenue"] == D(1600)
            assert result.totals["business_revenue"] is None
            assert result.totals["scenario_revenue"] is None
            assert result.totals["rebate"] == 0
            assert "PROVIDER_RATE_MISSING" in result.issues


def test_unknown_rebate_keeps_provider_income():
    from app.modules.business.calculation import business_amounts

    result = business_amounts(D(100), D(80), D("0.1"), None)
    assert result["provider_share"] == 8
    assert result["net_revenue"] == 72
    assert result["rebate"] is None
    assert result["business_revenue"] is None


def test_real_postgres_rate_writers_serialize_then_reject_stale_revision():
    from uuid import uuid4

    import pytest
    from fastapi import HTTPException
    from sqlalchemy import delete, text
    from sqlalchemy.exc import DBAPIError
    from sqlmodel import Session

    from app.core.context import TenantContext
    from app.core.db import engine
    from app.models import User
    from app.modules.accounts.models import TenantBC
    from app.modules.business.configuration import save_rate
    from app.modules.business.models import BusinessRateSchedule
    from app.modules.tenants.models import AuditEvent, Tenant, TenantMembership

    tenant_id, actor_id = uuid4(), uuid4()
    bc_id = f"rate-lock-{uuid4().hex}"
    context = TenantContext(tenant_id, actor_id, "tenant_admin")
    try:
        with Session(engine) as setup:
            setup.add_all(
                [
                    Tenant(id=tenant_id, name="rate-test"),
                    User(
                        id=actor_id,
                        username=f"rate-{actor_id.hex[:12]}",
                        hashed_password="test-only",
                        is_active=True,
                    ),
                ]
            )
            setup.flush()
            setup.add_all(
                [
                    TenantBC(tenant_id=tenant_id, bc_id=bc_id),
                    TenantMembership(
                        tenant_id=tenant_id,
                        user_id=actor_id,
                        role="tenant_admin",
                        active=True,
                    ),
                ]
            )
            setup.commit()
        args = {
            "context": context,
            "kind": "rebate",
            "scope_id": bc_id,
            "expected_revision": 0,
            "start_date": date(2026, 8, 1),
            "rate": D("0.06"),
        }
        with Session(engine) as first, Session(engine) as second:
            save_rate(first, **args)
            second.connection().execute(text("SET LOCAL lock_timeout='250ms'"))
            with pytest.raises(DBAPIError) as error:
                save_rate(second, **args)
            assert error.value.orig.sqlstate == "55P03"
            second.rollback()
            first.commit()
            with pytest.raises(HTTPException) as stale:
                save_rate(second, **args)
            assert stale.value.status_code == 409
            second.rollback()
        with Session(engine) as verify:
            assert (
                len(
                    verify.exec(
                        select(BusinessRateSchedule).where(
                            BusinessRateSchedule.tenant_id == tenant_id
                        )
                    ).all()
                )
                == 1
            )
    finally:
        with Session(engine) as cleanup:
            for model in (BusinessRateSchedule, AuditEvent, TenantMembership, TenantBC):
                cleanup.execute(delete(model).where(model.tenant_id == tenant_id))
            cleanup.execute(delete(Tenant).where(Tenant.id == tenant_id))
            cleanup.execute(delete(User).where(User.id == actor_id))
            cleanup.commit()


def test_migration_preserves_out_of_order_and_same_day_legacy_rates(
    session, business_case
):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    from app.modules.business.models import BusinessRate, BusinessRateSchedule
    from app.modules.business.rate_periods import RatePeriod, period_rate

    c = business_case
    transaction = session.begin_nested()
    try:
        for revision, day, rate in [
            (1, date(2026, 10, 1), "0.04"),
            (2, date(2026, 8, 1), "0.1"),
            (3, date(2026, 8, 1), "0.06"),
        ]:
            session.add(
                BusinessRate(
                    tenant_id=c.context.tenant_id,
                    actor_id=c.context.actor_id,
                    kind="provider",
                    scope_id=str(c.provider.id),
                    provider_id=c.provider.id,
                    effective_date=day,
                    rate=D(rate),
                    revision=revision,
                )
            )
        session.flush()
        session.execute(text("DROP TABLE business_rate_schedule"))
        migration = importlib.import_module(
            "app.alembic.versions.0102_business_rate_periods"
        )
        with Operations.context(MigrationContext.configure(session.connection())):
            migration.upgrade()
            nullable = session.execute(
                text("""SELECT bool_and(NOT a.attnotnull)
                FROM pg_attribute a WHERE a.attname='rebate_rate' AND
                (a.attrelid='business_day'::regclass OR a.attrelid IN
                 (SELECT inhrelid FROM pg_inherits WHERE inhparent='business_day'::regclass))""")
            ).scalar_one()
            assert nullable is True
            row = session.exec(
                select(BusinessRateSchedule).where(
                    BusinessRateSchedule.tenant_id == c.context.tenant_id
                )
            ).one()
            periods = [RatePeriod.model_validate(p) for p in row.periods]
            assert period_rate(periods, date(2026, 7, 31)) == 0
            assert period_rate(periods, date(2026, 8, 1)) == D("0.06")
            assert period_rate(periods, date(2026, 9, 30)) == D("0.06")
            assert period_rate(periods, date(2026, 10, 1)) == D("0.04")
            assert row.revision == 3
            migration.downgrade()
            migration.upgrade()
            assert (
                len(
                    session.exec(
                        select(BusinessRate).where(
                            BusinessRate.tenant_id == c.context.tenant_id
                        )
                    ).all()
                )
                == 3
            )
    finally:
        transaction.rollback()
        session.expire_all()


def test_history_pages_preserve_legacy_and_new_revisions(
    session, business_case, business_client
):
    from app.modules.business.configuration import save_rate
    from app.modules.business.models import BusinessRate, BusinessRateSchedule

    c = business_case
    for revision, rate in [(4, "0.1"), (5, "0.06")]:
        session.add(
            BusinessRate(
                tenant_id=c.context.tenant_id,
                actor_id=c.context.actor_id,
                kind="provider",
                scope_id=str(c.provider.id),
                provider_id=c.provider.id,
                effective_date=date(2026, 8, 1),
                revision=revision,
                rate=D(rate),
            )
        )
    session.add(
        BusinessRateSchedule(
            tenant_id=c.context.tenant_id,
            actor_id=c.context.actor_id,
            kind="provider",
            scope_id=str(c.provider.id),
            provider_id=c.provider.id,
            revision=5,
            periods=[{"start_date": "2026-08-01", "end_date": None, "rate": "0.06"}],
            operation={"action": "migration"},
        )
    )
    session.flush()
    for revision in (5, 6):
        save_rate(
            session,
            context=c.context,
            kind="provider",
            scope_id=str(c.provider.id),
            expected_revision=revision,
            start_date=date(2026, 10, 1),
            rate=D("0.04"),
            note="季度结算调整",
        )
    url = f"/api/tenants/{c.context.tenant_id}/business/rates/history"
    params = {"kind": "provider", "scope_id": str(c.provider.id), "limit": 2}
    first = business_client.get(url, params=params).json()
    assert [item["revision"] for item in first["items"]] == [7, 6]
    assert first["has_more"] is True
    assert first["items"][0]["actor_name"]
    assert first["items"][1]["previous_periods"][0]["rate"] == "0.06"
    last = business_client.get(url, params={**params, "offset": 2}).json()
    assert [item["revision"] for item in last["items"]] == [5, 4]
    assert all(item["legacy"] for item in last["items"])
    assert last["has_more"] is False


def test_rate_permissions_and_scope_isolation(
    business_client, session, business_case, other_context
):
    from app.modules.business.configuration import rate_on, save_rate
    from app.modules.providers.models import ProviderConnection

    c = business_case
    sibling = ProviderConnection(
        tenant_id=c.context.tenant_id, kind="jiashu", display_name="独立连接"
    )
    session.add(sibling)
    session.flush()
    save_rate(
        session,
        context=c.context,
        kind="provider",
        scope_id=str(c.provider.id),
        expected_revision=0,
        start_date=date(2026, 8, 1),
        rate=D("0.06"),
    )
    assert (
        rate_on(session, c.context.tenant_id, "provider", str(sibling.id), c.day) == 0
    )
    assert rate_on(session, c.context.tenant_id, "rebate", c.bc_id, c.day) == 0
    scope = {"kind": "provider", "scope_id": str(c.provider.id).upper()}
    url = f"/api/tenants/{c.context.tenant_id}/business/rates"
    assert business_client.get(url, params=scope).json()["revision"] == 1
    for path in ("", "/history"):
        assert (
            business_client.get(
                f"/api/tenants/{other_context.tenant_id}/business/rates{path}",
                params=scope,
            ).status_code
            == 403
        )
    from app.modules.tenants.models import TenantMembership

    member = session.get(TenantMembership, (c.context.tenant_id, c.context.actor_id))
    member.role = "operator"
    session.add(member)
    session.flush()
    assert business_client.get(f"{url}/history", params=scope).status_code == 403
    assert (
        business_client.post(
            f"{url}/preview",
            json={
                **scope,
                "expected_revision": 1,
                "start_date": "2026-09-01",
                "rate": "0.04",
            },
        ).status_code
        == 403
    )


def test_export_keeps_missing_rate_blank_and_old_snapshot_unchanged(
    session, business_case
):
    import csv
    import io

    from app.modules.business.configuration import save_mapping, save_rate
    from app.modules.business.exports import (
        create_export,
        export_content,
        process_pending_export,
    )
    from app.modules.business.queries import query_dashboard
    from app.modules.business.schemas import DashboardFilter
    from tests.modules.business.test_projection import build, seed_days

    c = business_case
    seed_days(session, c)
    for campaign in ("a", "b"):
        save_mapping(
            session,
            context=c.context,
            bc_id=c.bc_id,
            advertiser_id=c.advertiser_id,
            campaign_id=campaign,
            drama_id=c.drama.id,
        )
    save_rate(
        session,
        context=c.context,
        kind="rebate",
        scope_id=c.bc_id,
        expected_revision=0,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 1),
        rate=D("0.04"),
    )
    build(session, c)
    filters = DashboardFilter(
        bc_id=c.bc_id, start_date=date(2026, 11, 1), end_date=c.day
    )
    page = query_dashboard(session, context=c.context, filters=filters)
    from app.modules.business.models import BusinessDay

    details = session.exec(
        select(BusinessDay).where(
            BusinessDay.run_id == page.run_ids[0], BusinessDay.spend > 0
        )
    ).all()
    assert {row.stat_date: row.rebate_rate for row in details} == {
        date(2026, 11, 1): D("0.04"),
        date(2026, 11, 2): None,
    }
    assert page.totals["net_revenue"] == D(1600)
    assert page.totals["rebate"] is None
    export = create_export(
        session,
        context=c.context,
        filters=filters.model_copy(update={"run_ids": page.run_ids}),
    )
    process_pending_export(session)
    before = export_content(session, context=c.context, export_id=export.id)
    rows = list(csv.DictReader(io.StringIO(before.decode("utf-8-sig"))))
    assert rows and rows[0]["经营收入"] == "" and rows[0]["其他返点"] == ""
    assert "REBATE_RATE_MISSING" in rows[0]["数据说明"]
    save_rate(
        session,
        context=c.context,
        kind="rebate",
        scope_id=c.bc_id,
        expected_revision=1,
        start_date=date(2026, 10, 1),
        rate=D("0.06"),
    )
    build(session, c)
    assert query_dashboard(session, context=c.context, filters=filters).totals[
        "business_revenue"
    ] == D(1720)
    assert export_content(session, context=c.context, export_id=export.id) == before


def test_downgrade_refuses_to_discard_edited_schedule(session, business_case):
    import importlib

    import pytest
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import DBAPIError

    from app.modules.business.configuration import rate_on, save_rate

    c = business_case
    save_rate(
        session,
        context=c.context,
        kind="provider",
        scope_id=str(c.provider.id),
        expected_revision=0,
        start_date=date(2026, 8, 1),
        rate=D("0.06"),
    )
    migration = importlib.import_module(
        "app.alembic.versions.0102_business_rate_periods"
    )
    with pytest.raises(DBAPIError, match="forward migration required"):
        with (
            session.begin_nested(),
            Operations.context(MigrationContext.configure(session.connection())),
        ):
            migration.downgrade()
    assert rate_on(
        session, c.context.tenant_id, "provider", str(c.provider.id), c.day
    ) == D("0.06")
