"""真实 PostgreSQL：未知平台限流有界试探，跨领取保留退避与分页证据。"""

from datetime import datetime

import pytest

from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_material_resume import (
    Pages,
    claim,
    enqueue,
    facts,
    pages,
    run,
)


def _unknown():
    return AccountAdmissionDeferred(300_000, source="provider", reason="40100")


def _wait(factory, job_id, expected):
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        admission = job.progress["last_admission"]
        assert expected <= admission["retry_after_ms"] <= expected + 1000
        at = datetime.fromisoformat(admission["at"])
        assert (
            abs((job.due_at - at).total_seconds() * 1000 - admission["retry_after_ms"])
            < 10
        )
        assert job.attempts == 0
        assert job.progress["material_paging"]["observed_at"]
        return job.progress


@pytest.mark.parametrize("reason", ["40100", "http_429"])
def test_unknown_throttle_backoff_survives_claims_and_local_denial(
    committed_sync_seed, reason
):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(
        count=3,
        failure=(
            2,
            AccountAdmissionDeferred(300_000, source="provider", reason=reason),
        ),
    )
    assert run(factory, job, reader) == "WAITING"
    original = _wait(factory, job.id, 5000)
    assert [p.page for p in pages(factory, job.id)] == [1]
    reader.failure = (2, AccountAdmissionDeferred(1000, reason="endpoint_rate"))
    assert run(factory, claim(factory, job.id), reader) == "WAITING"
    with factory() as session:
        assert (
            session.get(ReportSyncJob, job.id).progress["provider_throttle"]
            == original["provider_throttle"]
        )
    reader.failure = (
        2,
        AccountAdmissionDeferred(300_000, source="provider", reason=reason),
    )
    for expected in (10_000, 20_000, 40_000, 60_000, 300_000, 300_000):
        assert run(factory, claim(factory, job.id), reader) == "WAITING"
        progress = _wait(factory, job.id, expected)
        assert (
            progress["material_paging"]["observed_at"]
            == original["material_paging"]["observed_at"]
        )
    assert facts(factory, seed.tenant_id) == []
    assert reader.calls.count(1) == 1
    reader.failure = None
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert len(facts(factory, seed.tenant_id)) == 3
    with factory() as session:
        assert "provider_throttle" not in session.get(ReportSyncJob, job.id).progress


def test_successful_page_resets_unknown_probe_budget(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(count=3, failure=(2, _unknown()))
    assert run(factory, job, reader) == "WAITING"
    assert run(factory, claim(factory, job.id), reader) == "WAITING"
    _wait(factory, job.id, 10_000)
    reader.failure = (3, _unknown())
    assert run(factory, claim(factory, job.id), reader) == "WAITING"
    progress = _wait(factory, job.id, 5000)
    assert progress["provider_throttle"]["attempts"] == 1
    assert [p.page for p in pages(factory, job.id)] == [1, 2]


@pytest.mark.parametrize(
    "reason,delay",
    [("qps", 1000), ("qpm", 300_000), ("qpd", 3_600_000), ("retry_after", 120_000)],
)
def test_explicit_provider_wait_is_never_shortened(committed_sync_seed, reason, delay):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    reader = Pages(
        count=3,
        failure=(2, AccountAdmissionDeferred(delay, source="provider", reason=reason)),
    )
    assert run(factory, job, reader) == "WAITING"
    progress = _wait(factory, job.id, delay)
    assert "provider_throttle" not in progress
    assert progress["last_admission"]["retry_after_ms"] == delay
