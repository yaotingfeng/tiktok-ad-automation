from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.modules.ai_workbench import evidence
from app.modules.ai_workbench.daily_report import (
    _evidence_ids_from_value,
    _next_trigger,
)


def test_cleanup_keeps_evidence_referenced_by_review_and_deletes_expired_unreferenced(
    monkeypatch,
):
    protected_id, stale_id, recent_expired_id, future_id = (uuid4() for _ in range(4))
    now = datetime(2026, 11, 12, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            id=protected_id,
            retention_until=now - timedelta(days=2),
            created_at=now - timedelta(days=10),
        ),
        SimpleNamespace(
            id=stale_id,
            retention_until=now - timedelta(days=2),
            created_at=now - timedelta(days=10),
        ),
        # This row is expired by its persisted fence even when the compatibility
        # argument remains at the default 365 days; the cleanup must not apply
        # that window a second time.
        SimpleNamespace(
            id=recent_expired_id,
            retention_until=now - timedelta(days=2),
            created_at=now - timedelta(days=10),
        ),
        SimpleNamespace(
            id=future_id,
            retention_until=now + timedelta(days=1),
            created_at=now - timedelta(days=10),
        ),
    ]

    class Result:
        def all(self):
            # Mirror the SQL fence: only rows whose persisted retention has passed.
            return [row for row in rows if row.retention_until <= now]

    class FakeSession:
        def __init__(self):
            self.deleted = []

        def exec(self, _query):
            return Result()

        def delete(self, row):
            self.deleted.append(row.id)

        def flush(self):
            return None

    monkeypatch.setattr(
        evidence, "referenced_evidence_ids", lambda *_args, **_kwargs: {protected_id}
    )
    session = FakeSession()
    assert evidence.cleanup_evidence(session, now=now, retention_days=365) == 2
    assert session.deleted == [stale_id, recent_expired_id]


def test_daily_report_uses_frozen_ids_from_execution_and_review_shapes():
    first, second = uuid4(), uuid4()
    value = {"evidence_id": first, "nested": [{"evidence_ids": [str(second)]}]}
    assert _evidence_ids_from_value(value) == {str(first), str(second)}


def test_next_trigger_chooses_earliest_future_build_or_rule():
    now = datetime(2026, 11, 12, 8, tzinfo=UTC)
    build = SimpleNamespace(
        id=uuid4(), status="PENDING", scheduled_at=now + timedelta(hours=2)
    )
    rule = SimpleNamespace(
        id=uuid4(), enabled=True, next_run_at=now + timedelta(hours=1)
    )
    assert _next_trigger(occurrences=[build], rules=[rule], now=now)["kind"] == "rule"


def test_partial_build_is_observed_terminal_and_material_state():
    from app.modules.ai_workbench.daily_report import TERMINAL_BUILD_STATUSES

    assert "PARTIAL" in TERMINAL_BUILD_STATUSES
