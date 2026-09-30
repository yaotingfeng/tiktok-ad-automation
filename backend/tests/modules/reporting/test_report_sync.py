from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.integrations.tiktok.contracts.common import CallEvidence
from app.integrations.tiktok.contracts.reporting import ReportPage, ReportRow
from app.modules.reporting.sync import collect_report_step


class _Result:
    def __init__(self, row):
        self.row = row

    def one_or_none(self):
        return self.row


class _Session:
    def __init__(self, row):
        self.row = row

    def exec(self, _statement):
        return _Result(self.row)

    def flush(self):
        return None

    def rollback(self):
        self.rolled_back = True


class _Reports:
    def check_task(self, task):
        return SimpleNamespace(task_id=task.task_id, advertiser_id=task.advertiser_id, status="RUNNING", query=task.query)


def test_collect_async_not_ready_is_bounded_wait():
    query = {
        "advertiser_id": "report-account",
        "report_contract": "basic_campaign",
        "metric_family": "delivery",
        "dimensions": ["campaign_id", "stat_time_day"],
        "metrics": ["spend"],
        "start_date": "2026-09-01",
        "end_date": "2026-09-01",
        "granularity": "DAY",
        "currency": "USD",
        "timezone": "UTC",
        "attribution": "default",
        "filter_ids": ["1"],
    }
    run = SimpleNamespace(
        id=uuid4(),
        claim_generation=1,
        status="WAITING_REMOTE",
        task_status="PROCESSING",
        task_id="task-1",
        query=query,
        coverage="PENDING",
        next_attempt_at=None,
    )
    result = collect_report_step(
        _Session(run), run_id=run.id, claim_generation=1, gateway=SimpleNamespace(reports=_Reports())
    )
    assert result == "WAIT"
    assert run.status == "WAITING_REMOTE"
    assert run.task_status == "RUNNING"
    assert run.next_attempt_at is not None


def test_stale_claim_is_fenced_without_mutating_new_generation():
    run = SimpleNamespace(
        id=uuid4(), claim_generation=2, status="RUNNING", coverage="PENDING", error_code=None
    )
    session = _Session(run)
    assert collect_report_step(session, run_id=run.id, claim_generation=1, gateway=object()) == "FAILED"
    assert run.status == "RUNNING"
    assert run.coverage == "PENDING"
    assert run.error_code is None


def test_sync_continuation_uses_persisted_next_page(monkeypatch):
    query = {
        "advertiser_id": "report-account",
        "report_contract": "basic_campaign",
        "metric_family": "delivery",
        "dimensions": ["campaign_id", "stat_time_day"],
        "metrics": ["spend"],
        "start_date": "2026-09-01",
        "end_date": "2026-09-01",
        "granularity": "DAY",
        "currency": "USD",
        "timezone": "UTC",
        "attribution": "default",
        "filter_ids": ["1"],
    }
    run = SimpleNamespace(
        id=uuid4(), claim_generation=1, status="RUNNING", task_status=None, task_id=None,
        query=query, next_page=1, coverage="PENDING", observed_at=None,
    )
    session = _Session(run)
    calls = []
    start = datetime(2026, 9, 1, tzinfo=UTC)

    def page(complete, number):
        return ReportPage(
            rows=(ReportRow(("campaign", "1"), start, start + timedelta(days=1), {"spend": Decimal("1")}, {"spend": "AVAILABLE"}),),
            next_page=None if complete else 2,
            complete=complete,
            evidence=CallEvidence(request_id=f"p{number}"),
            page=number,
        )

    class Reports:
        def read_page(self, received):
            calls.append(received.page)
            return page(received.page == 2, received.page)

    monkeypatch.setattr("app.modules.reporting.sync.stage_report_page", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.modules.reporting.sync.publish_report", lambda *args, **kwargs: setattr(run, "status", "COMPLETE"))
    gateway = SimpleNamespace(reports=Reports())
    assert collect_report_step(session, run_id=run.id, claim_generation=1, gateway=gateway) == "CONTINUE"
    assert run.next_page == 2
    assert collect_report_step(session, run_id=run.id, claim_generation=1, gateway=gateway) == "READY"
    assert calls == [1, 2]
