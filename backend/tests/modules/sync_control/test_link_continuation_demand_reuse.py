"""真实PG：无快照的补链续步承接新需求，普通财务快照仍不可复用。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.models import SyncExecutionScope, SyncRequirement
from tests.modules.reporting.test_jobs import _route


@pytest.fixture
def continuation(session, sync_seed):
    now = datetime.now(UTC) - timedelta(minutes=20)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "actor_id": sync_seed.actor_id,
        "bc_id": "bc-a",
        "kind": "material_links",
        "tier": "history",
        "advertiser_ids": ["acc-1"],
        "start": now.date() - timedelta(days=2),
        "end": now.date() - timedelta(days=2),
        "frozen_route": _route(sync_seed),
    }
    identity = enqueue_job(session, requested_at=now, **args)
    job = session.get(ReportSyncJob, identity)
    job.status, job.claim_generation, job.request_count = "WAITING", 3, 3
    job.claim_token = job.claimed_until = job.error_code = None
    job.progress = {
        "requested_at": now.isoformat(),
        "typed_link_search": True,
        "next_page": 6,
        "overview_ad_ids": ["ad"],
        "owner_lookup_first": False,
    }
    child = session.exec(
        select(SyncExecutionScope).where(SyncExecutionScope.report_job_id == identity)
    ).one()
    child.execution_generation = 3
    child.observed_at = now
    session.flush()
    return args, job, child, now


@pytest.mark.parametrize("cursor", ["overview", "locate", "identity"])
def test_new_demand_preserves_continuation_and_does_not_satisfy(
    session, continuation, cursor
):
    args, job, child, now = continuation
    if cursor == "locate":
        job.progress = {
            "requested_at": now.isoformat(),
            "typed_link_search": True,
            "locate_post": {
                "key": '["post", "VIDEO_SPARK_ADS"]',
                "mid": "post",
                "mtype": "VIDEO_SPARK_ADS",
                "candidates": ["ad"],
                "next_batch": 0,
                "search_stack": [],
            },
            "locate_queries": {"typed": True, "targets": [], "entries": {}},
        }
    elif cursor == "identity":
        job.progress = {
            "requested_at": now.isoformat(),
            "asset_identity_checked": ["mid"],
            "spark_identity_checked": [],
        }
    before = deepcopy(job.progress)
    due = job.due_at
    for minutes in (15, 16):
        assert (
            enqueue_job(session, requested_at=now + timedelta(minutes=minutes), **args)
            is None
        )
        session.refresh(child)
        requirement = session.get(SyncRequirement, child.requirement_id)
        assert requirement.satisfied_revision == 0
        assert child.frozen_revision == requirement.desired_revision
        assert child.frozen_requested_after == now + timedelta(minutes=minutes)
        assert child.observed_at is None and child.published_version is None
        assert job.progress == before and job.due_at == due
    assert (
        len(
            session.exec(
                select(ReportSyncJob).where(ReportSyncJob.tenant_id == job.tenant_id)
            ).all()
        )
        == 1
    )


@pytest.mark.parametrize(
    "block",
    [
        "snapshot",
        "material_paging",
        "basic_paging",
        "page",
        "running",
        "token",
        "claim_deadline",
        "head",
        "owner",
        "unknown",
        "invalid_page",
        "invalid_array",
        "invalid_locate",
        "other_kind",
        "route",
        "dates",
        "accounts",
        "campaigns",
    ],
)
def test_continuation_reuse_rejects_non_equivalent_or_observed_work(
    session, continuation, block
):
    from uuid import uuid4

    from app.modules.reporting.store_models import ReportSyncPage
    from app.modules.sync_control.models import SyncSchedule, SyncScope

    args, job, child, now = continuation
    if block in {"snapshot", "material_paging", "basic_paging", "unknown"}:
        key = "snapshot_observed_at" if block == "snapshot" else block
        job.progress = job.progress | {
            key: now.isoformat() if block == "snapshot" else {}
        }
    elif block == "page":
        session.add(
            ReportSyncPage(
                job_id=job.id,
                page=1,
                tenant_id=job.tenant_id,
                scope_key="snapshot",
                rows=[],
                row_keys=[],
                total_page=1,
                total_number=0,
            )
        )
    elif block == "running":
        job.status = "RUNNING"
    elif block == "token":
        job.claim_token = uuid4()
    elif block == "claim_deadline":
        job.claimed_until = datetime.now(UTC) + timedelta(minutes=1)
    elif block == "head":
        head = session.get(SyncSchedule, (child.scope_id, "directory"))
        head.lease_execution_id = job.id
        head.lease_execution_generation = job.claim_generation
        head.lease_token = uuid4()
        head.leased_until = datetime.now(UTC) + timedelta(minutes=1)
    elif block == "owner":
        session.get(SyncScope, child.scope_id).owner_epoch += 1
    elif block == "invalid_page":
        job.progress = job.progress | {"next_page": "6"}
    elif block == "invalid_array":
        job.progress = job.progress | {"spark_identity_checked": {}}
    elif block == "invalid_locate":
        job.progress = job.progress | {"locate_post": []}
    elif block == "other_kind":
        job.kind = args["kind"] = "ad_day"
    elif block == "route":
        args["frozen_route"] = args["frozen_route"] | {"binding_revision": 1}
    elif block == "dates":
        args["end"] += timedelta(days=1)
    elif block == "accounts":
        args["advertiser_ids"] = ["acc-1", "acc-2"]
    elif block == "campaigns":
        args["campaign_ids"] = ["another-campaign"]
    before = deepcopy(job.progress)
    session.flush()
    assert (
        enqueue_job(session, requested_at=now + timedelta(minutes=15), **args)
        is not None
    )
    session.refresh(child)
    assert child.frozen_revision == 1 and child.observed_at == now
    assert job.progress == before


def test_two_real_material_publications_keep_one_link_cursor(committed_sync_seed):
    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from app.modules.reporting.jobs import execute_job
    from app.modules.reporting.store_models import MaterialReportDay
    from tests.modules.reporting.claiming import claim_jobs
    from tests.modules.reporting.test_material_query import D, material_row

    seed, factory = committed_sync_seed
    args = {
        "tenant_id": seed.tenant_id,
        "actor_id": seed.actor_id,
        "bc_id": "bc-a",
        "tier": "material",
        "advertiser_ids": ["acc-1"],
        "start": D,
        "end": D,
        "frozen_route": _route(seed),
    }

    class Reader:
        def material_day(self, **_kwargs):
            return RawReportPage((material_row("post", "3"),), 1, 1, 1)

    original_id = None
    for iteration in range(2):
        with factory() as db:
            identity = enqueue_job(db, kind="material_day", **args)
            db.commit()
            claimed = claim_jobs(
                db, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
            )
            run = next(job for job in claimed if job.id == identity)
            token = run.claim_token
            db.commit()
        assert (
            execute_job(factory, job_id=identity, claim_token=token, reader=Reader())
            == "COMPLETE"
        )
        with factory() as db:
            links = db.exec(
                select(ReportSyncJob).where(
                    ReportSyncJob.tenant_id == seed.tenant_id,
                    ReportSyncJob.kind == "material_links",
                )
            ).all()
            assert len(links) == 1
            parent = links[0]
            child = db.exec(
                select(SyncExecutionScope).where(
                    SyncExecutionScope.report_job_id == parent.id
                )
            ).one()
            requirement = db.get(SyncRequirement, child.requirement_id)
            assert requirement.satisfied_revision == 0
            if iteration == 0:
                original_id = parent.id
                parent.status = "WAITING"
                parent.claim_generation = child.execution_generation = 1
                parent.error_code = parent.claim_token = parent.claimed_until = None
                parent.progress = parent.progress | {
                    "typed_link_search": True,
                    "next_page": 6,
                    "overview_ad_ids": ["ad"],
                }
                parent.due_at = datetime.now(UTC) + timedelta(hours=1)
                db.add(parent)
                db.add(child)
                db.commit()
            else:
                assert parent.id == original_id and parent.progress["next_page"] == 6
                assert child.frozen_revision == requirement.desired_revision == 2
            assert (
                db.get(MaterialReportDay, (seed.tenant_id, "acc-1", D, "post")).spend
                == 3
            )


@pytest.mark.parametrize(
    "invalid",
    [
        "empty_locate",
        "negative_offset",
        "zero_query_page",
        "checked_object",
        "cache_entries_list",
        "cache_pending_page",
        "asset_rows",
    ],
)
def test_malformed_nested_cursor_is_not_reused(session, continuation, invalid):
    args, job, child, now = continuation
    state = {
        "key": '["post", "VIDEO_SPARK_ADS"]',
        "mid": "post",
        "mtype": "VIDEO_SPARK_ADS",
        "candidates": ["ad"],
        "next_batch": 1,
        "search_stack": [{"ads": ["ad"], "confirmed": False}],
        "query": {"page": 2, "found": False, "signature": [2, 2]},
    }
    if invalid == "empty_locate":
        change = {"locate_post": {}}
    elif invalid == "negative_offset":
        change = {"locate_post": state | {"next_batch": -1}}
    elif invalid == "zero_query_page":
        change = {"locate_post": state | {"query": {"page": 0, "found": False}}}
    elif invalid == "checked_object":
        change = {"checked_ids": [{}]}
    elif invalid == "cache_entries_list":
        change = {"locate_queries": {"typed": True, "targets": [], "entries": []}}
    elif invalid == "cache_pending_page":
        change = {
            "locate_queries": {
                "typed": True,
                "targets": [],
                "entries": {},
                "pending": {
                    "key": "hash",
                    "at": 1,
                    "next_page": 0,
                    "signature": [2, 2],
                    "seen": [],
                    "members": [],
                },
            }
        }
    else:
        change = {
            "spark_asset_content": {
                "scope": "scope",
                "at": now.isoformat(),
                "rows": [{}],
            }
        }
    job.progress = job.progress | change
    before = deepcopy(job.progress)
    session.flush()
    assert (
        enqueue_job(session, requested_at=now + timedelta(minutes=15), **args)
        is not None
    )
    assert child.frozen_revision == 1 and job.progress == before


def test_valid_nested_paging_evidence_is_preserved(session, continuation):
    args, job, child, now = continuation
    identity = '["post", "VIDEO_SPARK_ADS"]'
    job.progress = {
        "typed_link_search": True,
        "locate_post": {
            "key": identity,
            "mid": "post",
            "mtype": "VIDEO_SPARK_ADS",
            "candidates": ["ad"],
            "preferred_count": 1,
            "next_batch": 1,
            "search_stack": [{"ads": ["ad"], "confirmed": False}],
            "query": {"page": 2, "found": False, "signature": [2, 2]},
        },
        "locate_queries": {
            "typed": True,
            "targets": [identity],
            "entries": {
                "complete-query": {"at": now.timestamp(), "members": [identity]}
            },
            "pending": {
                "key": "current-query",
                "at": now.timestamp(),
                "next_page": 2,
                "signature": [2, 2],
                "seen": ["hash"],
                "members": [identity],
            },
        },
    }
    before = deepcopy(job.progress)
    session.flush()
    assert (
        enqueue_job(session, requested_at=now + timedelta(minutes=15), **args) is None
    )
    assert job.progress == before and child.frozen_revision == 2


def test_new_fact_and_changed_scan_evidence_cannot_complete_reused_parent(
    committed_sync_seed,
):
    from app.modules.reporting.jobs import execute_job
    from app.modules.reporting.link_queries import material_identity_key
    from app.modules.reporting.material_links import _scan_scope, _scan_signature
    from app.modules.reporting.store_models import MaterialLinkScan
    from tests.modules.reporting.claiming import claim_jobs
    from tests.modules.reporting.test_material_query import (
        D,
        material_row,
        publish,
        use,
    )

    seed, factory = committed_sync_seed
    old = datetime.now(UTC) - timedelta(minutes=20)
    args = {
        "tenant_id": seed.tenant_id,
        "actor_id": seed.actor_id,
        "bc_id": "bc-a",
        "kind": "material_links",
        "tier": "history",
        "advertiser_ids": ["acc-1"],
        "start": D,
        "end": D,
        "frozen_route": _route(seed),
    }
    with factory() as db:
        publish(db, seed, "acc-1", [material_row("old-post", "3")])
        identity = enqueue_job(db, requested_at=old, **args)
        job = db.get(ReportSyncJob, identity)
        job.status, job.claim_generation = "WAITING", 2
        job.claim_token = job.claimed_until = job.error_code = None
        job.progress = {
            "typed_link_search": True,
            "overview_complete": True,
            "overview_ad_ids": [],
            "owner_lookup_first": True,
            "owner_lookup_finished": True,
            "checked_ids": [material_identity_key("old-post", "VIDEO_SPARK_ADS")],
            "scan_evidence_ids": ["old-post"],
            "scan_evidence_signature": _scan_signature(
                db, seed.tenant_id, "acc-1", ["old-post"], D, D, ()
            ),
            "scan_evidence_at": old.isoformat(),
        }
        child = db.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.report_job_id == identity
            )
        ).one()
        child.execution_generation = 2
        # 同一次完整日报现在多出有价值的新post；不能拿旧checked/概览完成签新需求。
        publish(
            db,
            seed,
            "acc-1",
            [material_row("old-post", "3"), material_row("new-post", "1")],
        )
        assert enqueue_job(db, requested_at=datetime.now(UTC), **args) is None
        db.commit()
        run = next(
            j
            for j in claim_jobs(
                db, now=datetime.now(UTC) + timedelta(seconds=1), limit=10
            )
            if j.id == identity
        )
        token = run.claim_token
        db.commit()

    class NoNetwork:
        def material_ad_links(self, **_kwargs):
            pytest.fail(
                "old overview completion must first become honest dependency WAIT"
            )

        def material_ad_posts(self, **_kwargs):
            pytest.fail("no directory/report ad candidate exists")

    assert (
        execute_job(factory, job_id=identity, claim_token=token, reader=NoNetwork())
        == "WAITING"
    )
    with factory() as db:
        job = db.get(ReportSyncJob, identity)
        child = db.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.report_job_id == identity
            )
        ).one()
        requirement = db.get(SyncRequirement, child.requirement_id)
        assert requirement.satisfied_revision == 0 and child.status == "PENDING"
        assert any("new-post" in item for item in job.progress["waiting_ids"])
        assert (
            "overview_complete" not in job.progress
            and "checked_ids" not in job.progress
        )
        scan = db.get(
            MaterialLinkScan, (seed.tenant_id, "acc-1", _scan_scope(D, D, ()))
        )
        assert scan.evidence_signature is None and scan.scanned_at == old
        # 当前缺口真实补齐后才能以当前DB复核完成；正向关系不靠需求时间伪造。
        use(
            db,
            seed,
            adv="acc-1",
            ad="old-ad",
            video="old-video",
            ad_material="old-native",
            post="old-post",
        )
        use(
            db,
            seed,
            adv="acc-1",
            ad="new-ad",
            video="new-video",
            ad_material="new-native",
            post="new-post",
        )
        assert enqueue_job(db, requested_at=datetime.now(UTC), **args) is None
        job.due_at = datetime.now(UTC)
        db.add(job)
        from app.modules.sync_control.schedule import record_report_outcome

        record_report_outcome(db, job)
        db.commit()
        run = next(
            j
            for j in claim_jobs(
                db, now=datetime.now(UTC) + timedelta(seconds=1), limit=10
            )
            if j.id == identity
        )
        token = run.claim_token
        db.commit()
    assert (
        execute_job(factory, job_id=identity, claim_token=token, reader=NoNetwork())
        == "COMPLETE"
    )
    with factory() as db:
        child = db.exec(
            select(SyncExecutionScope).where(
                SyncExecutionScope.report_job_id == identity
            )
        ).one()
        requirement = db.get(SyncRequirement, child.requirement_id)
        assert child.status == "PUBLISHED"
        assert requirement.satisfied_revision == requirement.desired_revision == 3
        assert child.observed_at >= child.frozen_requested_after > old


def test_large_legal_progress_builds_each_lookup_set_once(
    session, continuation, monkeypatch, record_property
):
    import builtins
    import json
    from collections import Counter
    from time import monotonic

    from app.modules.sync_control import report_demand

    args, job, child, now = continuation
    candidates = [f"ad-{i}" for i in range(2000)]
    targets = [json.dumps([f"post-{i}", "VIDEO_SPARK_ADS"]) for i in range(5000)]
    job.progress = {
        "typed_link_search": True,
        "locate_post": {
            "key": targets[0],
            "mid": "post-0",
            "mtype": "VIDEO_SPARK_ADS",
            "candidates": candidates,
            "next_batch": 2000,
            "search_stack": [
                {"ads": candidates[i : i + 100], "confirmed": False}
                for i in range(0, 1600, 100)
            ],
        },
        "locate_queries": {
            "typed": True,
            "targets": targets,
            "entries": {
                f"query-{i}": {"at": now.timestamp(), "members": [targets[i]]}
                for i in range(128)
            },
            "pending": {
                "key": "pending",
                "at": now.timestamp(),
                "next_page": 2,
                "signature": [2, 2],
                "seen": ["row-hash"],
                "members": [targets[-1]],
            },
        },
    }
    before = deepcopy(job.progress)
    session.flush()
    walks = Counter()

    def measured_set(value=()):
        if type(value) is list and len(value) in {2000, 5000}:
            walks[len(value)] += 1
        return builtins.set(value)

    monkeypatch.setattr(report_demand, "set", measured_set, raising=False)
    started = monotonic()
    assert (
        enqueue_job(session, requested_at=now + timedelta(minutes=15), **args) is None
    )
    elapsed = monotonic() - started
    assert job.progress == before and child.frozen_revision == 2
    record_property("large_progress_elapsed_s", round(elapsed, 4))
    record_property("lookup_set_builds", dict(walks))
    assert walks == {2000: 1, 5000: 1}
    assert elapsed < 3
