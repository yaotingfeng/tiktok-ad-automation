"""后台关联量子服从持久grant；独立连接核对每步提交、围栏与真实费用。"""

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
from tests.modules.reporting.test_material_query import material_row


@pytest.fixture
def quantum_case(committed_sync_seed, redis_client, monkeypatch, request):  # noqa: F811
    seed, factory = committed_sync_seed
    from zoneinfo import ZoneInfo

    from app.modules.reporting.publish import AccountMeta, publish_material_day

    mode = getattr(request, "param", {})
    day = (
        datetime.now(UTC).astimezone(ZoneInfo("Asia/Hong_Kong")).date()
        if mode.get("current")
        else D
    )
    kind = mode.get("kind", "material_links")
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"links-quantum-{uuid4()}")
    refills = []
    monkeypatch.setattr(
        job_tasks, "_dispatch_pending", lambda *_a, **_k: refills.append(True) or 0
    )
    with factory() as db:
        row = material_row("unresolved", "1")
        row["dimensions"]["stat_time_day"] = str(day)
        publish_material_day(
            db,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
            start=day,
            end=day,
            rows=[row],
        )
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
        # 默认由旧调度器发三步grant；新consumer不能扩大原预留或未知成本。
        with monkeypatch.context() as legacy_dispatcher:
            if not mode.get("production_budget"):
                legacy_dispatcher.setattr(jobs, "MATERIAL_LINK_STEPS_PER_QUANTUM", 3)
            run = claim_jobs(db, now=datetime.now(UTC) + timedelta(seconds=1), limit=1)[
                0
            ]
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

        def material_ad_links(self, *, advertiser_id, page, ad_ids):
            with case.factory() as db:
                run = db.get(ReportSyncJob, case.identity)
                assert run.progress.get("next_page", 1) == page
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
            with physical_attempt_scope(client, report_endpoint("material_links")):
                connection.request()
            self.request_count += 1
            if retry_first and page == 1:
                with physical_attempt_scope(client, report_endpoint("material_links")):
                    connection.request()
                self.request_count += 1
            if after:
                after(page)
            return RawReportPage((), page, total_pages, total_pages)

    @contextmanager
    def reader(**kw):
        deadlines.append(kw["deadline"])
        yield Reader()

    monkeypatch.setattr(job_tasks, "_reader_for", reader)
    return seen, deadlines, tokens


def test_new_grant_commits_three_pages_and_releases_once(quantum_case, monkeypatch):
    case = quantum_case
    with case.factory() as db:
        assert _ledger(db, case).reserved_units == 3
    seen, deadlines, tokens = _install_reader(monkeypatch, case)
    assert _invoke(case) == "WAITING"
    assert seen == [1, 2, 3] and len(deadlines) == 1 and len(set(tokens)) == 1
    assert case.refills == [True]
    with case.factory() as db:
        run = db.get(ReportSyncJob, case.identity)
        assert run.progress["next_page"] == 4 and run.request_count == 3
        assert run.claim_token is None and run.claimed_until is None
        head = db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == case.seed.tenant_id)
        ).one()
        assert head.lease_token is None and head.active
        ledger = _ledger(db, case)
        assert ledger.observed_units == 3 and ledger.closed


@pytest.mark.parametrize("quantum_case", [{"production_budget": True}], indirect=True)
def test_ready_link_grant_advances_ten_pages_before_one_refill(
    quantum_case, monkeypatch
):
    case = quantum_case
    with case.factory() as db:
        assert _ledger(db, case).reserved_units == 10
    seen, deadlines, tokens = _install_reader(monkeypatch, case, total_pages=20)
    assert _invoke(case) == "WAITING"
    assert seen == list(range(1, 11))
    assert len(deadlines) == 1 and len(set(tokens)) == 1 and case.refills == [True]
    with case.factory() as db:
        run = db.get(ReportSyncJob, case.identity)
        assert run.progress["next_page"] == 11 and run.request_count == 10
        assert run.claim_token is None
        assert _ledger(db, case).observed_units == 10 and _ledger(db, case).closed


@pytest.mark.parametrize("budget", [1, 0, None])
def test_consumer_obeys_original_grant_budget(quantum_case, monkeypatch, budget):
    case = quantum_case
    with case.factory() as db:
        ledger = _ledger(db, case)
        if budget is None:
            db.delete(ledger)
        else:
            ledger.reserved_units = budget
        db.commit()
    seen, _, _ = _install_reader(monkeypatch, case)
    assert _invoke(case) == "WAITING"
    assert seen == ([1] if budget == 1 else [])
    with case.factory() as db:
        run = db.get(ReportSyncJob, case.identity)
        assert run.claim_token is None and run.claimed_until is None
        head = db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == case.seed.tenant_id)
        ).one()
        assert head.lease_token is None


def test_second_page_error_keeps_first_commit_and_sent_cost(quantum_case, monkeypatch):
    case = quantum_case

    def fail(page):
        if page == 2:
            raise DomainError("tiktok_response_error", "local transport error")

    seen, _, _ = _install_reader(monkeypatch, case, before=fail)
    assert _invoke(case) == "WAITING"
    assert seen == [1, 2]
    with case.factory() as db:
        run = db.get(ReportSyncJob, case.identity)
        assert run.progress["next_page"] == 2 and run.request_count == 1
        assert run.error_code == "tiktok_response_error"
        assert run.claim_token is None
        assert _ledger(db, case).observed_units == 1


@pytest.mark.parametrize(
    "change",
    ["owner", "generation", "token", "head", "head_error", "head_admission", "route"],
)
def test_successor_or_route_change_between_pages_stops_quantum(
    quantum_case, monkeypatch, change
):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.sync_control.models import SyncExecutionScope, SyncScope

    case = quantum_case
    expected = []

    def mutate(page):
        if page != 2:
            return
        with case.factory() as db:
            run = db.get(ReportSyncJob, case.identity)
            assert run.progress["next_page"] == 2 and run.request_count == 1
            head = db.exec(
                select(SyncSchedule).where(SyncSchedule.lease_execution_id == run.id)
            ).one()
            if change == "owner":
                child = db.exec(
                    select(SyncExecutionScope).where(
                        SyncExecutionScope.report_job_id == run.id
                    )
                ).one()
                db.get(SyncScope, child.scope_id).owner_epoch += 1
            elif change == "generation":
                run.claim_generation += 1
            elif change == "token":
                run.claim_token = uuid4()
            elif change.startswith("head"):
                head.lease_token = uuid4()
            else:
                db.get(
                    BCConnectionBinding,
                    (case.seed.tenant_id, "bc-a", case.seed.connection.id),
                ).revision += 1
            db.commit()
            expected.append(
                (
                    run.status,
                    run.claim_token,
                    run.claim_generation,
                    run.progress["next_page"],
                    head.lease_token,
                )
            )
        if change == "head_error":
            raise DomainError(
                "tiktok_response_error", "request failed after newer grant"
            )
        if change == "head_admission":
            raise AccountAdmissionDeferred(1000)

    seen, _, _ = _install_reader(monkeypatch, case, before=mutate)
    assert _invoke(case) == "FAILED"
    assert seen == [1, 2]
    with case.factory() as db:
        run = db.get(ReportSyncJob, case.identity)
        head = db.exec(
            select(SyncSchedule).where(SyncSchedule.tenant_id == case.seed.tenant_id)
        ).one()
        assert run.progress["next_page"] == 2
        if change != "route":
            assert (
                run.status,
                run.claim_token,
                run.claim_generation,
                run.progress["next_page"],
                head.lease_token,
            ) == expected[0]
        else:
            assert run.error_code == "route_binding_changed" and run.claim_token is None


@pytest.mark.parametrize(
    "quantum_case", [{}, {"production_budget": True}], indirect=True
)
def test_absolute_quantum_deadline_yields_after_committed_step(
    quantum_case, monkeypatch
):
    case = quantum_case
    advanced = []
    real = datetime

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + timedelta(seconds=21 if advanced else 0)

    monkeypatch.setattr(jobs, "datetime", Clock)
    seen, deadlines, _ = _install_reader(
        monkeypatch, case, before=lambda page: advanced.append(page)
    )
    before = real.now(UTC)
    assert _invoke(case) == "WAITING"
    assert seen == [1] and len(deadlines) == 1
    assert before < deadlines[0] <= real.now(UTC) + timedelta(seconds=20)
    with case.factory() as db:
        assert db.get(ReportSyncJob, case.identity).progress["next_page"] == 2


def test_actual_retry_cost_consumes_remaining_step_budget(quantum_case, monkeypatch):
    case = quantum_case
    seen, _, _ = _install_reader(monkeypatch, case, retry_first=True)
    assert _invoke(case) == "WAITING"
    assert seen == [1, 2]
    with case.factory() as db:
        assert _ledger(db, case).observed_units == 3
        assert db.get(ReportSyncJob, case.identity).request_count == 3


def test_dependency_wait_stops_quantum_and_preserves_missing_fact(
    quantum_case, monkeypatch
):
    case = quantum_case
    seen, _, _ = _install_reader(monkeypatch, case, total_pages=1)
    assert _invoke(case) == "WAITING"
    assert seen == [1]
    with case.factory() as db:
        run = db.get(ReportSyncJob, case.identity)
        assert (
            run.error_code == "material_identity_unavailable"
            and run.due_at > datetime.now(UTC)
        )
        assert run.claim_token is None and run.progress["waiting_ids"] == ["unresolved"]


@pytest.mark.parametrize("quantum_case", [{"current": True}], indirect=True)
def test_current_day_links_remain_single_step(quantum_case, monkeypatch):
    case = quantum_case
    with case.factory() as db:
        assert db.get(ReportSyncJob, case.identity).execution_pool == "live-report"
        assert _ledger(db, case).reserved_units == 1
    seen, _, _ = _install_reader(monkeypatch, case)
    assert _invoke(case) == "WAITING" and seen == [1]


def test_old_consumer_of_new_grant_settles_only_actual_cost(quantum_case, monkeypatch):
    from app.modules.sync_control.costs import settle_idle_costs

    case = quantum_case
    original = job_tasks.execute_job

    def old_consumer(*args, **kwargs):
        kwargs["max_steps"] = 1
        return original(*args, **kwargs)

    monkeypatch.setattr(job_tasks, "execute_job", old_consumer)
    seen, _, _ = _install_reader(monkeypatch, case)
    assert _invoke(case) == "WAITING" and seen == [1]
    assert settle_idle_costs(case.engine, budget_seconds=2) == 1
    with case.factory() as db:
        ledger = _ledger(db, case)
        assert (
            ledger.reserved_units == 3
            and ledger.observed_units == ledger.charged_units == 1
            and ledger.closed
        )


def test_empty_budget_cannot_clear_replacement_head(quantum_case, monkeypatch):
    case = quantum_case
    new_token = uuid4()
    with case.factory() as db:
        _ledger(db, case).reserved_units = 0
        head = db.exec(
            select(SyncSchedule).where(SyncSchedule.lease_execution_id == case.identity)
        ).one()
        head.lease_token = new_token
        db.commit()
    seen, _, _ = _install_reader(monkeypatch, case)
    assert _invoke(case) == "FAILED" and seen == []
    with case.factory() as db:
        assert db.get(ReportSyncJob, case.identity).claim_token == case.token
        assert (
            db.exec(
                select(SyncSchedule).where(
                    SyncSchedule.lease_execution_id == case.identity
                )
            )
            .one()
            .lease_token
            == new_token
        )


@pytest.mark.parametrize("interrupted", [False, True])
def test_error_after_send_keeps_committed_page_and_actual_or_unknown_cost(
    quantum_case, monkeypatch, interrupted
):
    from billiard.exceptions import SoftTimeLimitExceeded

    case = quantum_case

    def fail(page):
        if page == 2:
            if interrupted:
                raise SoftTimeLimitExceeded()
            raise DomainError(
                "read_deadline_exceeded", "response arrived past deadline"
            )

    seen, _, _ = _install_reader(monkeypatch, case, after=fail)
    if interrupted:
        with pytest.raises(SoftTimeLimitExceeded):
            _invoke(case)
    else:
        assert _invoke(case) == "WAITING"
    assert seen == [1, 2]
    with case.factory() as db:
        run = db.get(ReportSyncJob, case.identity)
        ledger = _ledger(db, case)
        assert run.progress["next_page"] == 2 and ledger.observed_units == 2
        assert ledger.closed is (not interrupted)
        if interrupted:
            assert ledger.charged_units == 3 and run.request_count == 1
        else:
            assert run.request_count == 2 and run.claim_token is None


def test_already_resolved_links_complete_without_extra_reads(quantum_case, monkeypatch):
    from sqlalchemy import delete

    from app.modules.reporting.store_models import MaterialReportDay

    case = quantum_case
    with case.factory() as db:
        db.execute(
            delete(MaterialReportDay).where(
                MaterialReportDay.tenant_id == case.seed.tenant_id
            )
        )
        db.commit()
    seen, _, _ = _install_reader(monkeypatch, case)
    assert _invoke(case) == "COMPLETE" and seen == [] and case.refills == [True]
    with case.factory() as db:
        run = db.get(ReportSyncJob, case.identity)
        assert run.claim_token is None and run.claimed_until is None
        assert _ledger(db, case).observed_units == 0


@pytest.mark.parametrize("quantum_case", [{"kind": "ad_day"}], indirect=True)
def test_non_link_job_keeps_original_single_execution(quantum_case, monkeypatch):
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
        assert run.status == "RUNNING" and run.progress["next_page"] == 2
        ledger = _ledger(db, case)
        assert ledger.observed_units == 2 and ledger.charged_units == 3
        assert not ledger.closed


@pytest.mark.parametrize("missing", [False, True])
def test_budget_removed_at_reader_entry_cannot_clear_successor(
    quantum_case, monkeypatch, missing
):
    case = quantum_case
    expected = []

    @contextmanager
    def reader(**_kwargs):
        with case.factory() as db:
            ledger = _ledger(db, case)
            if missing:
                db.delete(ledger)
            else:
                ledger.reserved_units = 0
            run = db.get(ReportSyncJob, case.identity)
            head = db.exec(
                select(SyncSchedule).where(SyncSchedule.lease_execution_id == run.id)
            ).one()
            head.lease_token = uuid4()
            db.commit()
            expected.append((run.claim_token, head.lease_token))
        yield object()  # 没有预算，任何HTTP方法调用都是错误。

    monkeypatch.setattr(job_tasks, "_reader_for", reader)
    assert _invoke(case) == "FAILED"
    with case.factory() as db:
        run = db.get(ReportSyncJob, case.identity)
        head = db.exec(
            select(SyncSchedule).where(SyncSchedule.lease_execution_id == run.id)
        ).one()
        assert (run.claim_token, head.lease_token) == expected[0]
        assert run.status == "RUNNING" and run.request_count == 0
