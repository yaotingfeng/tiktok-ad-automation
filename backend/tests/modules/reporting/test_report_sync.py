from types import SimpleNamespace
from uuid import uuid4

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
