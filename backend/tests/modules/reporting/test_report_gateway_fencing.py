"""报表执行服从持久grant；独立连接核对提交、后继租约与未知成本围栏。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting import job_tasks, jobs
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.models import SyncCostLedger, SyncSchedule
from tests.modules.reporting.claiming import claim_jobs
from tests.modules.reporting.sync_conftest import committed_sync_seed  # noqa: F401
from tests.modules.reporting.test_jobs import D, _route


@pytest.fixture
def quantum_case(committed_sync_seed, redis_client, monkeypatch, request):  # noqa: F811
    seed, factory = committed_sync_seed
    from zoneinfo import ZoneInfo

    mode = getattr(request, "param", {})
    day = (
        datetime.now(UTC).astimezone(ZoneInfo("Asia/Hong_Kong")).date()
        if mode.get("current")
        else D
    )
    kind = mode.get("kind", "creative_day")
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"report-fencing-{uuid4()}")
    refills = []
    monkeypatch.setattr(
        job_tasks, "_dispatch_pending", lambda *_a, **_k: refills.append(True) or 0
    )
    with factory() as db:
        identity = jobs.enqueue_job(
            db,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind=kind,
            tier="material",
            advertiser_ids=["acc-1"],
            start=day,
            end=day,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        db.commit()
        run = claim_jobs(db, now=datetime.now(UTC) + timedelta(seconds=1), limit=1)[0]
        db.commit()
        token, generation, engine = run.claim_token, run.claim_generation, db.get_bind()
    return SimpleNamespace(
        seed=seed,
        factory=factory,
        identity=identity,
        token=token,
        generation=generation,
        engine=engine,
        redis=redis_client,
        refills=refills,
    )


def _ledger(db, case):
    return db.exec(
        select(SyncCostLedger).where(
            SyncCostLedger.report_job_id == case.identity,
            SyncCostLedger.generation == case.generation,
        )
    ).one()


def _invoke(case):
    return job_tasks.run_job_step(
        database_engine=case.engine,
        redis_client=case.redis,
        job_id=case.identity,
        claim_token=case.token,
    )


def _install_reader(
    monkeypatch, case, *, before=None, after=None, total_pages=10, retry_first=False
):
    from app.integrations.tiktok.attempt_observer import physical_attempt_scope
    from app.integrations.tiktok.transport_diagnostics import install_transport_tracking
    from app.modules.sync_control.schedule import report_endpoint

    class Connection:
        def request(self):
            return "local response"

    class Pool:
        ConnectionCls = Connection

    client = SimpleNamespace(
        rest_client=SimpleNamespace(
            pool_manager=SimpleNamespace(pool_classes_by_scheme={"https": Pool})
        )
    )
    install_transport_tracking(client)
    connection = client.rest_client.pool_manager.pool_classes_by_scheme[
        "https"
    ].ConnectionCls()
    seen, deadlines, tokens = [], [], []

    class Reader:
        request_count = 0

        def creative_day(self, *, advertiser_ids, start, end, page, campaign_ids=()):
            with case.factory() as db:
                run = db.get(ReportSyncJob, case.identity)
                assert (
                    run.status == "RUNNING" and run.claim_generation == case.generation
                )
                tokens.append(run.claim_token)
                head = db.exec(
                    select(SyncSchedule).where(
                        SyncSchedule.lease_execution_id == case.identity
                    )
                ).one()
                assert head.lease_token == case.token
            seen.append(page)
            if before:
                before(page)
            with physical_attempt_scope(client, report_endpoint("creative_day")):
                connection.request()
            self.request_count += 1
            if retry_first and page == 1:
                with physical_attempt_scope(client, report_endpoint("creative_day")):
                    connection.request()
                self.request_count += 1
            if after:
                after(page)
            return RawReportPage(
                (
                    {
                        "dimensions": {
                            "ad_id": f"creative-{page}",
                            "stat_time_day": str(D),
                        },
                        "metrics": {
                            "campaign_id": "c1",
                            "adgroup_id": "g1",
                            "spend": "1",
                        },
                    },
                ),
                page,
                total_pages,
                total_pages,
            )

    @contextmanager
    def reader(**kw):
        deadlines.append(kw["deadline"])
        yield Reader()

    monkeypatch.setattr(job_tasks, "_reader_for", reader)
    return seen, deadlines, tokens


@pytest.mark.parametrize("quantum_case", [{"kind": "ad_day"}], indirect=True)
def test_report_job_keeps_original_single_execution(quantum_case, monkeypatch):
    case = quantum_case
    seen = []

    class Reader:
        def ad_day(self, **_kwargs):
            seen.append(True)
            return RawReportPage((), 1, 0, 0)

    @contextmanager
    def reader(**_kwargs):
        yield Reader()

    monkeypatch.setattr(job_tasks, "_reader_for", reader)
    assert _invoke(case) == "COMPLETE" and seen == [True]
    with case.factory() as db:
        assert _ledger(db, case).reserved_units == 1


@pytest.mark.parametrize("admission", [False, True])
def test_gateway_open_error_cannot_clear_new_head(quantum_case, monkeypatch, admission):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    case = quantum_case
    expected = []

    @contextmanager
    def reader(**_kwargs):
        with case.factory() as db:
            run = db.get(ReportSyncJob, case.identity)
            head = db.exec(
                select(SyncSchedule).where(
                    SyncSchedule.lease_execution_id == case.identity
                )
            ).one()
            head.lease_token = uuid4()
            db.commit()
            expected.append((run.claim_token, head.lease_token))
        if admission:
            raise AccountAdmissionDeferred(1000)
        raise DomainError("read_deadline_exceeded", "gateway open ended")
        yield

    monkeypatch.setattr(job_tasks, "_reader_for", reader)
    assert _invoke(case) == "FAILED"
    with case.factory() as db:
        run = db.get(ReportSyncJob, case.identity)
        head = db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == case.seed.tenant_id)
        ).one()
        assert (
            run.status == "RUNNING"
            and (run.claim_token, head.lease_token) == expected[0]
        )


@pytest.mark.parametrize("change", ["generation", "token", "owner", "head"])
def test_soft_interrupt_with_successor_keeps_unknown_cost(
    quantum_case, monkeypatch, change
):
    from billiard.exceptions import SoftTimeLimitExceeded

    from app.modules.sync_control.models import SyncExecutionScope, SyncScope

    case = quantum_case
    expected = []

    def interrupt(page):
        if page != 2:
            return
        with case.factory() as db:
            run = db.get(ReportSyncJob, case.identity)
            head = db.exec(
                select(SyncSchedule).where(SyncSchedule.lease_execution_id == run.id)
            ).one()
            if change == "generation":
                run.claim_generation += 1
            elif change == "token":
                run.claim_token = uuid4()
            elif change == "head":
                head.lease_token = uuid4()
            else:
                child = db.exec(
                    select(SyncExecutionScope).where(
                        SyncExecutionScope.report_job_id == run.id
                    )
                ).one()
                db.get(SyncScope, child.scope_id).owner_epoch += 1
            db.commit()
            expected.append((run.claim_token, run.claim_generation, head.lease_token))
        raise SoftTimeLimitExceeded()

    seen, _, _ = _install_reader(monkeypatch, case, after=interrupt)
    with pytest.raises(SoftTimeLimitExceeded):
        _invoke(case)
    assert seen == [1, 2]
    with case.factory() as db:
        run = db.get(ReportSyncJob, case.identity)
        head = db.exec(
            select(SyncSchedule).where(SyncSchedule.lease_execution_id == run.id)
        ).one()
        assert (run.claim_token, run.claim_generation, head.lease_token) == expected[0]
        assert run.status == "RUNNING"
        ledger = _ledger(db, case)
        assert ledger.observed_units == 2
        assert ledger.charged_units == ledger.reserved_units == 1
        assert not ledger.closed
