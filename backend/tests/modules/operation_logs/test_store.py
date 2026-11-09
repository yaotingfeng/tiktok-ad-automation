from datetime import UTC, date, datetime
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

from sqlmodel import select

from app.modules.operation_logs.models import (
    OperationCoverage,
    OperationExport,
    OperationLogEvent,
)
from app.modules.operation_logs.parser import parse_export, utc_window
from app.modules.operation_logs.store import publish_export
from tests.modules.operation_logs.test_parser import export


def publish(
    session,
    case,
    content,
    observed=None,
    *,
    end=date(2026, 11, 1),
    timezone="UTC",
    tail_end=None,
):
    job = SimpleNamespace(
        id=uuid4(),
        tenant_id=case.context.tenant_id,
        bc_id=case.bc_id,
        advertiser_ids=["report-account"],
        frozen_route={},
        start_date=date(2026, 11, 1),
        end_date=end,
        progress={"operation_log": {"task_id": "task-test"}},
    )
    receipt = SimpleNamespace(key="private/test", size=len(content.encode()))
    changed = publish_export(
        session,
        job=job,
        content_digest=sha256(content.encode()).hexdigest(),
        receipt=receipt,
        events=parse_export(content),
        start_at=utc_window(date(2026, 11, 1), end, timezone)[0],
        end_at=tail_end or utc_window(date(2026, 11, 1), end, timezone)[1],
        timezone=timezone,
        observed_at=observed or datetime(2026, 11, 2, 12, tzinfo=UTC),
    )
    session.flush()
    return job, changed


def test_atomic_empty_replay_and_revision(session, report_case):
    first, changed = publish(session, report_case, export())
    assert changed
    key = (report_case.context.tenant_id, "report-account", date(2026, 11, 1))
    coverage = session.get(OperationCoverage, key)
    assert coverage.revision == 1
    second, changed = publish(session, report_case, export())
    assert not changed
    assert coverage.revision == 1
    detail = [
        {
            "action": "Change ",
            "name": "minimum ROAS",
            "before_after": [{"before": "1.2", "after": "1.1"}],
        }
    ]
    third, changed = publish(session, report_case, export(detail, detail))
    assert changed
    assert coverage.revision == 2
    assert (
        len(
            session.exec(
                select(OperationLogEvent).where(OperationLogEvent.export_id == third.id)
            ).all()
        )
        == 2
    )
    assert session.get(OperationExport, first.id) is not None
    assert (
        session.get(OperationCoverage, (uuid4(), "report-account", date(2026, 11, 1)))
        is None
    )


def test_late_old_export_cannot_replace_new_coverage(session, report_case):
    newest, _ = publish(session, report_case, export())
    old, changed = publish(
        session, report_case, export(), datetime(2026, 11, 2, 10, tzinfo=UTC)
    )
    assert not changed
    assert (
        session.get(
            OperationCoverage,
            (report_case.context.tenant_id, "report-account", date(2026, 11, 1)),
        ).export_id
        == newest.id
    )
