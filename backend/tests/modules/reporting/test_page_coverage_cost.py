"""七日多账户覆盖只处理当前账户/行的状态，不按每行重扫全账户集合。"""

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.modules.reporting import dimension_query as dq
from app.modules.reporting import material_query as mq
from app.modules.reporting.row_set import SnapshotMember
from app.modules.reporting.schemas import ReportingFilter

D = date(2026, 10, 1)
ACCOUNT_COUNT = 520


@pytest.fixture
def coverage_page_case(session, sync_seed):
    session.connection().execute(
        text("""INSERT INTO advertiser_account
        (tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
        SELECT :tenant,'coverage-'||i,'account-'||i,'USD','UTC','UNKNOWN',false
        FROM generate_series(0,519) i"""),
        {"tenant": sync_seed.tenant_id},
    )
    session.connection().execute(
        text("""INSERT INTO report_sync_state
        (tenant_id,advertiser_id,report_kind,stat_date,status,row_count,publication_version,updated_at,last_success_at)
        SELECT :tenant,'coverage-'||i,kind,CAST(:start AS date)+day,'COMPLETE',0,1,now(),now()
        FROM generate_series(0,519) i CROSS JOIN generate_series(0,6) day
        CROSS JOIN (VALUES('ad_day'),('material_day')) k(kind)
        WHERE i<>519 OR day<>6"""),
        {"tenant": sync_seed.tenant_id, "start": D},
    )
    session.connection().execute(
        text("""UPDATE read_bucket_state SET derived_revision=source_revision,
        derived_status='COMPLETE',derived_observed_at=observed_at
        WHERE tenant_id=:tenant AND kind='material_day'"""),
        {"tenant": sync_seed.tenant_id},
    )
    return session, sync_seed


def _ref(seed, index, kind):
    return {
        "tenant_id": str(seed.tenant_id),
        "advertiser_id": f"coverage-{index}",
        "kind": kind,
        "remote_id": f"remote-{index}",
    }


def _material_members(seed):
    return [
        SnapshotMember(
            json.dumps(["material", f"vid-{i}"]),
            (_ref(seed, i, "ad"),),
            (
                {
                    "ad_ref": _ref(seed, i, "ad"),
                    "platform_material_id": f"vid-{i}",
                    "ad_material_id": None,
                    "material_type": "VIDEO",
                },
            ),
        )
        for i in range(ACCOUNT_COUNT)
    ]


def test_drama_coverage_input_is_linear_in_accounts_and_days(
    coverage_page_case, monkeypatch
):
    session, seed = coverage_page_case
    original = dq._coverage
    sizes = []

    def tracked(states, **kwargs):
        count = len(list(states))
        sizes.append(count)
        assert count <= len(kwargs["advertisers"]) * 7
        return original(states, **kwargs)

    monkeypatch.setattr(dq, "_coverage", tracked)
    members = [
        SnapshotMember(
            f"drama-{start}",
            tuple(_ref(seed, i, "campaign") for i in range(start, start + 10)),
            (),
        )
        for start in range(0, ACCOUNT_COUNT, 10)
    ]
    rows = dq.materialize_rows(
        session,
        tenant_id=seed.tenant_id,
        filters=ReportingFilter(
            dimension="drama", start_date=D, end_date=D + timedelta(days=6)
        ),
        members=members,
        advertisers=[f"coverage-{i}" for i in range(ACCOUNT_COUNT)],
    )
    assert len(rows) == 52
    assert sum(sizes) <= 2 * ACCOUNT_COUNT * 7
    assert rows[0].coverage["status"] == "COMPLETE"
    assert rows[-1].coverage["incomplete_advertiser_ids"] == ["coverage-519"]
    assert rows[-1].metric_buckets[0].availability["spend"] == "PARTIAL"


def test_material_projection_states_are_indexed_once(coverage_page_case, monkeypatch):
    session, seed = coverage_page_case
    original = mq.projected_states
    visits = []

    class CountedRows(list):
        def __iter__(self):
            for state in super().__iter__():
                visits.append(1)
                yield state

    def tracked(*args, **kwargs):
        states, projections = original(*args, **kwargs)
        return states, CountedRows(projections)

    monkeypatch.setattr(mq, "projected_states", tracked)
    rows = mq.materialize_material_rows(
        session,
        tenant_id=seed.tenant_id,
        filters=ReportingFilter(
            dimension="material", start_date=D, end_date=D + timedelta(days=6)
        ),
        members=_material_members(seed),
        advertisers=[f"coverage-{i}" for i in range(ACCOUNT_COUNT)],
    )
    assert len(rows) == ACCOUNT_COUNT
    assert len(visits) <= ACCOUNT_COUNT * 7
    assert rows[-1].coverage["incomplete_advertiser_ids"] == ["coverage-519"]


def test_material_hydration_states_use_page_scope_including_frozen_uses(
    coverage_page_case, monkeypatch
):
    session, seed = coverage_page_case
    original = mq.projected_states
    scopes = []

    def tracked(*args, **kwargs):
        scopes.append(kwargs["advertisers"])
        return original(*args, **kwargs)

    monkeypatch.setattr(mq, "projected_states", tracked)
    rows = mq.materialize_material_rows(
        session,
        tenant_id=seed.tenant_id,
        filters=ReportingFilter(
            dimension="material", start_date=D, end_date=D + timedelta(days=6)
        ),
        members=_material_members(seed)[:1],
        advertisers=[f"coverage-{i}" for i in range(ACCOUNT_COUNT)],
    )
    assert rows[0].display["account_id"] == "coverage-0"
    assert rows[0].coverage["status"] == "COMPLETE"
    assert scopes == [["coverage-0"]]
