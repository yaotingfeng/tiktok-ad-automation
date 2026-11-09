"""真实来源版本刷新后只重算候选尾部，保留旧正式结果。"""

from copy import deepcopy
from datetime import date, timedelta

import pytest
from sqlmodel import select

from app.modules.business.models import (
    BusinessDay,
    BusinessPublication,
    BusinessRun,
    BusinessSummaryDay,
)
from app.modules.business.projection import advance_run
from app.modules.reporting.store_models import CampaignReportDay, ReportSyncState
from tests.modules.business.test_prefix_progress import NOW, _changed_candidate


def before_verification(session, run):
    for _ in range(200):
        if run.phase == "SUBSIDY" and run.cursor > run.end_date:
            return
        assert not advance_run(session, run.id, now=NOW)
    pytest.fail("calculation did not reach the final source fence")


def advance_to(session, run, phase):
    for _ in range(300):
        if run.phase == phase:
            return
        assert run.status not in {"SUPERSEDED", "COMPLETE", "FAILED"}
        advance_run(session, run.id, now=NOW)
    pytest.fail("bounded state machine did not reach the requested phase")


def content(session, model, run_id, *, before=None):
    stmt = select(model).where(model.run_id == run_id)
    if before is not None:
        stmt = stmt.where(model.stat_date < before)
    return sorted(
        [
            {k: v for k, v in row.model_dump().items() if k != "run_id"}
            for row in session.exec(stmt).all()
        ],
        key=lambda row: repr(sorted(row.items())),
    )


def changed_source(session, c, day=date(2026, 11, 2)):
    state = session.get(
        ReportSyncState, (c.context.tenant_id, c.advertiser_id, "ad_day", day)
    )
    state.publication_version += 1
    state.updated_at += timedelta(seconds=1)
    session.add(state)
    session.flush()


def test_changed_tail_reuses_candidate_and_equals_fresh_build(
    session, business_case, redis_client
):
    assert redis_client.ping()
    c = business_case
    old, run = _changed_candidate(session, c)
    before_verification(session, run)
    dirty = date(2026, 11, 2)
    preserved = content(session, BusinessDay, run.id, before=dirty)
    changed_source(session, c, dirty)
    fact = session.get(
        CampaignReportDay, (c.context.tenant_id, c.advertiser_id, dirty, "b")
    )
    session.delete(fact)
    session.flush()
    key = (run.tenant_id, run.bc_id, run.quarter)
    advance_to(session, run, "REBASE")
    assert run.coverage["_rebase"]["dirty"] == str(dirty)
    assert session.get(BusinessPublication, key).run_id == old.id
    assert content(session, BusinessDay, run.id, before=dirty) == preserved
    for _ in range(300):
        if advance_run(session, run.id, now=NOW):
            break
        assert session.get(BusinessPublication, key).run_id == old.id
    assert run.status == "COMPLETE"
    assert session.get(BusinessPublication, key).run_id == run.id
    assert content(session, BusinessDay, run.id, before=dirty) == preserved
    expected = {
        model: content(session, model, run.id)
        for model in (BusinessDay, BusinessSummaryDay)
    }
    fresh = BusinessRun(
        tenant_id=run.tenant_id,
        bc_id=run.bc_id,
        actor_id=run.actor_id,
        quarter=run.quarter,
        end_date=run.end_date,
        scope_revision=run.scope_revision,
        cursor=run.quarter,
        inputs={**deepcopy(run.inputs), "dirty_start": str(run.quarter)},
        source_signature=run.source_signature,
    )
    session.add(fresh)
    session.flush()
    for _ in range(150):
        if advance_run(session, fresh.id, now=NOW):
            break
    assert fresh.status == "COMPLETE"
    for model in expected:
        assert content(session, model, fresh.id) == expected[model]


def test_cleanup_crash_rolls_back_rows_and_cursor(session, business_case, monkeypatch):
    from app.modules.business import projection

    old, run = _changed_candidate(session, business_case)
    before_verification(session, run)
    changed_source(session, business_case, date(2026, 11, 1))
    advance_to(session, run, "REBASE")
    monkeypatch.setattr(projection, "BASE_BATCH", 1)
    prior = deepcopy(run.coverage)
    rows = content(session, BusinessDay, run.id)
    with pytest.raises(RuntimeError, match="crash"):
        with session.begin_nested():
            advance_run(session, run.id, now=NOW)
            session.flush()
            raise RuntimeError("crash")
    session.refresh(run)
    assert run.coverage == prior
    assert content(session, BusinessDay, run.id) == rows
    advance_run(session, run.id, now=NOW)
    assert len(content(session, BusinessDay, run.id)) == len(rows) - 1
    assert (
        session.get(BusinessPublication, (run.tenant_id, run.bc_id, run.quarter)).run_id
        == old.id
    )


@pytest.mark.parametrize(
    "field", ["publication_version", "updated_at", "observed_at", "row_count", "status"]
)
def test_every_state_change_invalidates_the_exact_day(session, business_case, field):
    c = business_case
    old, run = _changed_candidate(session, c)
    before_verification(session, run)
    day = date(2026, 11, 2)
    state = session.get(
        ReportSyncState, (c.context.tenant_id, c.advertiser_id, "ad_day", day)
    )
    if field in {"publication_version", "row_count"}:
        setattr(state, field, getattr(state, field) + 1)
    elif field == "status":
        state.status = "STALE"
    else:
        setattr(state, field, state.updated_at + timedelta(seconds=10))
    session.add(state)
    session.flush()
    advance_to(session, run, "REBASE")
    assert run.coverage["_rebase"]["dirty"] == str(day)
    assert (
        session.get(BusinessPublication, (run.tenant_id, run.bc_id, run.quarter)).run_id
        == old.id
    )


def test_second_source_refresh_keeps_same_candidate_unpublished(session, business_case):
    old, run = _changed_candidate(session, business_case)
    before_verification(session, run)
    changed_source(session, business_case)
    advance_to(session, run, "REBASE")
    advance_to(session, run, "VERIFY")
    changed_source(session, business_case, date(2026, 11, 1))
    assert not advance_run(session, run.id, now=NOW)
    assert run.phase == "REBASE" and run.coverage["_rebase"]["rounds"] == 2
    assert run.coverage["_rebase"]["dirty"] == "2026-11-01"
    assert (
        session.get(BusinessPublication, (run.tenant_id, run.bc_id, run.quarter)).run_id
        == old.id
    )


@pytest.mark.parametrize(
    "structure", ["catalog", "account", "scope", "actor", "malformed"]
)
def test_structural_change_never_reuses_candidate(
    session, business_case, other_context, structure
):
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.business.models import BusinessScope

    c = business_case
    old, run = _changed_candidate(session, c)
    before_verification(session, run)
    advance_run(session, run.id, now=NOW)
    assert run.phase == "VERIFY"
    if structure == "catalog":
        c.drama.title += "-changed"
        session.add(c.drama)
    elif structure == "account":
        account = session.get(AdvertiserAccount, (c.context.tenant_id, c.advertiser_id))
        account.currency = "EUR"
        session.add(account)
    elif structure in {"scope", "actor"}:
        scope = session.get(BusinessScope, (run.tenant_id, run.bc_id))
        if structure == "scope":
            scope.revision += 1
        else:
            scope.actor_id = other_context.actor_id
        session.add(scope)
    else:
        value = deepcopy(run.inputs)
        value["states"][f"{c.advertiser_id}/2026-11-02"].pop("version")
        run.inputs = value
        changed_source(session, c)
    session.flush()
    assert advance_run(session, run.id, now=NOW)
    assert run.status == "SUPERSEDED"
    assert (
        session.get(BusinessPublication, (run.tenant_id, run.bc_id, run.quarter)).run_id
        == old.id
    )


def test_complete_through_qualification_change_is_dirty(session, business_case):
    from app.modules.business.prefix import rebase_dirty_start

    _, run = _changed_candidate(session, business_case)
    source = deepcopy(run.inputs)
    previous = deepcopy(run.inputs)
    previous["accounts"][business_case.advertiser_id]["complete_through"] = "2026-11-01"
    run.inputs = previous
    assert rebase_dirty_start(run, source) == date(2026, 11, 2)


def test_verify_obeys_existing_global_snapshot_singleton(session, business_case):
    from sqlalchemy import text

    from app.core.db import engine
    from app.modules.business.prefix import SNAPSHOT_LOCK
    from app.modules.business.projection import request_rebuild
    from tests.modules.business.test_projection import seed_days

    c = business_case
    seed_days(session, c)
    run = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=NOW,
    )[0]
    before_verification(session, run)
    advance_run(session, run.id, now=NOW)
    before = content(session, BusinessDay, run.id)
    with engine.connect() as holder, holder.begin():
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": SNAPSHOT_LOCK}
        )
        assert not advance_run(session, run.id, now=NOW)
    assert run.phase == "VERIFY" and run.status == "RUNNING"
    assert content(session, BusinessDay, run.id) == before
    assert advance_run(session, run.id, now=NOW)
    assert run.status == "COMPLETE"


def test_summary_cleanup_is_bounded_and_replayable(session, business_case, monkeypatch):
    from app.modules.business import projection

    old, run = _changed_candidate(session, business_case)
    before_verification(session, run)
    changed_source(session, business_case, date(2026, 11, 1))
    formal = content(session, BusinessSummaryDay, old.id)
    advance_to(session, run, "REBASE")
    monkeypatch.setattr(projection, "BASE_BATCH", 1)
    while run.coverage["_rebase"]["stage"] == "day":
        advance_run(session, run.id, now=NOW)
    rows = content(session, BusinessSummaryDay, run.id)
    assert len(rows) > 1
    prior = deepcopy(run.coverage)
    with pytest.raises(RuntimeError, match="summary crash"):
        with session.begin_nested():
            advance_run(session, run.id, now=NOW)
            session.flush()
            raise RuntimeError("summary crash")
    session.refresh(run)
    assert run.coverage == prior
    assert content(session, BusinessSummaryDay, run.id) == rows
    advance_run(session, run.id, now=NOW)
    assert len(content(session, BusinessSummaryDay, run.id)) == len(rows) - 1
    assert content(session, BusinessSummaryDay, old.id) == formal


def test_verify_persists_new_snapshot_and_cleanup_marker_atomically(
    session, business_case
):
    _, run = _changed_candidate(session, business_case)
    before_verification(session, run)
    advance_run(session, run.id, now=NOW)
    old_inputs, old_signature, old_coverage = (
        deepcopy(run.inputs),
        run.source_signature,
        deepcopy(run.coverage),
    )
    changed_source(session, business_case)
    with pytest.raises(RuntimeError, match="verify crash"):
        with session.begin_nested():
            assert not advance_run(session, run.id, now=NOW)
            assert run.phase == "REBASE"
            raise RuntimeError("verify crash")
    session.refresh(run)
    assert run.phase == "VERIFY"
    assert (
        run.inputs == old_inputs
        and run.source_signature == old_signature
        and run.coverage == old_coverage
    )
    assert not advance_run(session, run.id, now=NOW)
    assert run.phase == "REBASE" and run.source_signature != old_signature


def test_worker_verifies_only_once_with_heavy_budget(
    session, business_case, monkeypatch
):
    from contextlib import contextmanager
    from time import monotonic

    from app.modules.business import projection, tasks
    from tests.modules.business.test_projection import seed_days

    c = business_case
    seed_days(session, c)
    run = projection.request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=c.day,
        now=NOW,
    )[0]
    before_verification(session, run)
    advance_run(session, run.id, now=NOW)
    second = BusinessRun(
        tenant_id=run.tenant_id,
        bc_id=run.bc_id,
        actor_id=run.actor_id,
        quarter=run.quarter,
        end_date=run.end_date,
        scope_revision=run.scope_revision,
        phase="SNAPSHOT",
        cursor=run.quarter,
    )
    session.add(second)
    session.flush()
    real_source = projection.source_snapshot
    budgets = []

    def measured_source(db, *args, **kwargs):
        budget = db.connection().info["tkada_query_budget"]
        budgets.append(min(budget.deadlines) - monotonic())
        return real_source(db, *args, **kwargs)

    @contextmanager
    def local_session():
        yield session

    monkeypatch.setattr(tasks, "_work_session", local_session)
    monkeypatch.setattr(projection, "source_snapshot", measured_source)
    assert tasks.work_business() == 1
    assert run.status == "COMPLETE"
    assert second.status == "QUEUED" and second.phase == "SNAPSHOT"
    assert len(budgets) == 1 and 15 < budgets[0] <= 18


def test_cleanup_never_exceeds_500_even_with_larger_base_batch(
    session, business_case, monkeypatch
):
    from sqlalchemy import inspect, text

    from app.modules.business import projection

    old, run = _changed_candidate(session, business_case)
    before_verification(session, run)
    changed_source(session, business_case, date(2026, 11, 1))
    advance_to(session, run, "REBASE")
    columns = [column.name for column in inspect(BusinessDay).local_table.columns]
    values = [
        "'extra-'||g::text" if column == "campaign_id" else "seed." + column
        for column in columns
    ]
    session.execute(
        text(f"""INSERT INTO business_day ({",".join(columns)})
      SELECT {",".join(values)} FROM (SELECT * FROM business_day WHERE run_id=:run AND stat_date='2026-11-01' LIMIT 1) seed
      CROSS JOIN generate_series(1,1001) g"""),
        {"run": run.id},
    )
    monkeypatch.setattr(projection, "BASE_BATCH", 1000)
    before = len(content(session, BusinessDay, run.id))
    official = content(session, BusinessDay, old.id)
    advance_run(session, run.id, now=NOW)
    assert before - len(content(session, BusinessDay, run.id)) == 500
    assert run.coverage["_rebase"]["day_deleted"] == 500
    assert run.phase == "REBASE" and run.coverage["_rebase"]["stage"] == "day"
    assert content(session, BusinessDay, old.id) == official
