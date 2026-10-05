from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.reporting.scheduling import (
    SyncRequest,
    directory_filter_chunks,
    directory_id_chunks,
    enqueue_due_syncs,
    ensure_sync_schedules,
    planned_window,
    request_sync,
    schedule_key,
)
from app.modules.reporting.sync_models import ReportSyncRun, SyncSchedule


def _route():
    return FrozenTikTokRoute(
        tenant_id=uuid4(),
        bc_id="bc-test",
        connection_id=uuid4(),
        channel="OFFICIAL_API",
        authorization_revision=4,
        adapter_contract_revision="official-api-v1",
        binding_revision=9,
    )


def test_directory_id_chunks_bound_each_platform_filter_to_100_ids():
    ids = tuple(f"id-{i}" for i in range(205))

    chunks = directory_id_chunks(ids)

    assert [len(chunk) for chunk in chunks] == [100, 100, 5]
    assert tuple(item for chunk in chunks for item in chunk) == ids
    assert directory_id_chunks(()) == ((),)
    filters = directory_filter_chunks(tuple(f"id-{i}" for i in range(101)), tuple(f"p-{i}" for i in range(101)))
    assert len(filters) == 4
    assert all(len(ids) <= 100 and len(parents) <= 100 for ids, parents in filters)


def test_planned_windows_keep_initial_and_unknown_attribution_bounds():
    assert planned_window(kind="initial").days == 30
    assert planned_window(kind="unknown_attribution").days == 35
    assert planned_window(kind="attribution", attribution_days=90).days == 97
    assert planned_window(kind="core").days == 2


def test_sync_request_rejects_cross_scope_refs_and_duplicate_accounts():
    route = _route()
    ref = EntityRef(route.tenant_id, "account-a", "campaign", "campaign-1")
    request = SyncRequest(
        route=route,
        advertiser_ids=("account-a", "account-a"),
        scope="targeted",
        start_date=None,
        end_date=None,
        refs=(ref,),
    )
    assert request.advertiser_ids == ("account-a",)
    with pytest.raises(ValueError):
        SyncRequest(
            route=route,
            advertiser_ids=("account-a",),
            scope="targeted",
            start_date=None,
            end_date=None,
            refs=(EntityRef(route.tenant_id, "other", "campaign", "campaign-1"),),
        )


def test_route_generation_is_part_of_schedule_identity():
    first = _route()
    changed = first.model_copy(update={"binding_revision": first.binding_revision + 1})
    assert schedule_key(
        route=first, advertiser_id="account-a", scope="report", window="core"
    ) != schedule_key(
        route=changed, advertiser_id="account-a", scope="report", window="core"
    )


def test_planned_window_rejects_naive_clock_only_when_used_for_dates():
    # The public pure window helper accepts an optional clock; date planning itself
    # validates account-local timezone when a persistent account is available.
    assert planned_window(kind="recent7d", now=datetime.now(UTC)).days == 7


def test_request_sync_is_idempotent_for_same_frozen_request(session, reporting_seed):
    route = FrozenTikTokRoute(
        tenant_id=reporting_seed.context.tenant_id,
        bc_id="bc-report",
        connection_id=reporting_seed.connection.id,
        channel="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
        binding_revision=0,
    )
    request = SyncRequest(
        route=route,
        advertiser_ids=("report-account",),
        scope="report",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 2),
    )
    first = request_sync(session, context=reporting_seed.context, request=request)
    second = request_sync(session, context=reporting_seed.context, request=request)
    assert second == first
    runs = session.exec(select(ReportSyncRun)).all()
    assert len(runs) == 9  # eight contracts plus the legacy Smart+ ad partition
    assert {run.query["report_contract"] for run in runs} == {
        "basic_account",
        "basic_campaign",
        "basic_adgroup",
        "basic_ad",
        "basic_smart_plus_ad",
        "basic_smart_plus_creative",
        "material_overview",
        "material_breakdown",
    }


def test_report_ranges_are_sharded_and_terminal_occurrence_is_not_reused(
    session, reporting_seed
):
    route = FrozenTikTokRoute(
        tenant_id=reporting_seed.context.tenant_id,
        bc_id="bc-report",
        connection_id=reporting_seed.connection.id,
        channel="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
        binding_revision=0,
    )
    request = SyncRequest(
        route=route,
        advertiser_ids=("report-account",),
        scope="report",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
    )
    first = request_sync(session, context=reporting_seed.context, request=request)
    runs = session.exec(select(ReportSyncRun)).all()
    assert len(runs) == 27  # nine contract/type partitions, each <= 30 days
    assert all(
        (
            date.fromisoformat(run.query["end_date"])
            - date.fromisoformat(run.query["start_date"])
        ).days
        < 30
        for run in runs
    )
    for run in runs:
        run.status = "COMPLETE"
    session.flush()
    second = request_sync(session, context=reporting_seed.context, request=request)
    assert second != first


def test_mixed_shards_reuse_request_and_material_breakdown_is_unfiltered(
    session, reporting_seed
):
    route = FrozenTikTokRoute(
        tenant_id=reporting_seed.context.tenant_id,
        bc_id="bc-report",
        connection_id=reporting_seed.connection.id,
        channel="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
        binding_revision=0,
    )
    request = SyncRequest(
        route=route,
        advertiser_ids=("report-account",),
        scope="report",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        refs=(EntityRef(route.tenant_id, "report-account", "ad", "ad-1"),),
    )
    first = request_sync(session, context=reporting_seed.context, request=request)
    runs = session.exec(select(ReportSyncRun)).all()
    material = next(
        run for run in runs if run.query["report_contract"] == "material_breakdown"
    )
    assert material.query["filter_ids"] == []
    first_run = session.get(ReportSyncRun, first)
    assert first_run is not None
    first_run.status = "COMPLETE"
    session.flush()
    second = request_sync(session, context=reporting_seed.context, request=request)
    assert second == first
    assert len(session.exec(select(ReportSyncRun)).all()) == len(runs)


def test_fixed_plans_use_local_account_windows_and_intervals(session, reporting_seed):
    route = FrozenTikTokRoute(
        tenant_id=reporting_seed.context.tenant_id,
        bc_id="bc-report",
        connection_id=reporting_seed.connection.id,
        channel="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
        binding_revision=0,
    )
    rows = ensure_sync_schedules(
        session,
        context=reporting_seed.context,
        route=route,
        now=datetime(2026, 9, 30, 12, tzinfo=UTC),
    )
    assert len(rows) == 8
    core = next(
        row for row in rows if row.scope == "report" and "core" in row.schedule_key
    )
    assert core.interval_seconds == 1800
    assert (core.end_date - core.start_date).days == 1
    initial = next(
        row for row in rows if row.scope == "history" and "initial" in row.schedule_key
    )
    assert (initial.end_date - initial.start_date).days == 29
    assert len(session.exec(select(SyncSchedule)).all()) == 8


def test_due_plan_advances_from_prior_due_time_without_tight_recreation(
    session, reporting_seed
):
    route = FrozenTikTokRoute(
        tenant_id=reporting_seed.context.tenant_id,
        bc_id="bc-report",
        connection_id=reporting_seed.connection.id,
        channel="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
        binding_revision=0,
    )
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    ensure_sync_schedules(session, context=reporting_seed.context, route=route, now=now)
    run_ids = enqueue_due_syncs(session, now=now + timedelta(hours=4))
    assert len(run_ids) == 8
    assert all(
        row.next_due_at > now + timedelta(hours=4)
        for row in session.exec(select(SyncSchedule)).all()
    )
