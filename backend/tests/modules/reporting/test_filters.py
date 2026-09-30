from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.core.context import TenantContext
from app.models import User
from app.modules.ads.models import AdObject
from app.modules.reporting.filters import apply_authorized_scope, compile_filter
from app.modules.reporting.query_models import QuerySnapshot, read_snapshot
from app.modules.reporting.schemas import ReportingFilter
from app.modules.tenants.models import TenantMembership


def test_filter_literals_and_scope(report_case):
    report_case.seed_campaign("MAX_% 甲", "report-account", "10", "3")
    compiled = compile_filter(
        ReportingFilter(
            dimension="campaign",
            start_date=date(2026, 9, 30),
            end_date=date(2026, 9, 30),
            query="MAX_% 甲",
        )
    )
    assert compiled.keywords == ("MAX_%", "甲")
    assert compiled.like_patterns == (r"%MAX\_\%%", r"%甲%")
    statement = select(AdObject).where(
        AdObject.tenant_id == report_case.context.tenant_id,
        AdObject.kind == "campaign",
    )
    statement = compiled.apply(statement, name_column=AdObject.name)
    statement = apply_authorized_scope(
        report_case.session,
        statement,
        context=report_case.context,
        bc_id=report_case.bc_id,
        advertiser_column=AdObject.advertiser_id,
    )
    assert [row.name for row in report_case.session.exec(statement)] == ["MAX_% 甲"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_date": date(2026, 10, 1), "end_date": date(2026, 9, 30)},
        {"min_spend": Decimal("NaN")},
        {"max_spend": Decimal("Infinity")},
        {"sort_by": "unknown"},
    ],
)
def test_filter_rejects_invalid_ranges_values_and_sort(kwargs):
    base = {
        "dimension": "campaign",
        "start_date": date(2026, 9, 30),
        "end_date": date(2026, 9, 30),
    }
    with pytest.raises(ValueError):
        compile_filter(ReportingFilter(**(base | kwargs)))


def test_snapshot_read_is_tenant_bc_scoped_and_viewer_allowed(report_case):
    snapshot = QuerySnapshot(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=report_case.context.actor_id,
        advertiser_ids=["report-account"],
        filters={"dimension": "campaign"},
        filter_digest="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    report_case.session.add(snapshot)
    viewer = User(
        id=uuid4(), username=f"viewer-{uuid4().hex[:10]}", hashed_password="test-only"
    )
    report_case.session.add(viewer)
    report_case.session.add(
        TenantMembership(
            tenant_id=report_case.context.tenant_id,
            user_id=viewer.id,
            role="viewer",
            active=True,
        )
    )
    report_case.session.flush()
    assert read_snapshot(
        report_case.session,
        context=TenantContext(
            tenant_id=report_case.context.tenant_id, actor_id=viewer.id, role="viewer"
        ),
        bc_id=report_case.bc_id,
        snapshot_id=snapshot.id,
    ).id == snapshot.id
    with pytest.raises(HTTPException) as error:
        read_snapshot(
            report_case.session,
            context=report_case.other_context,
            bc_id=report_case.bc_id,
            snapshot_id=snapshot.id,
        )
    assert error.value.status_code == 404
