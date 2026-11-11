from datetime import UTC, date, datetime, time
from uuid import uuid4

import pytest

from app.modules.ai_workbench.build_handoff import BuildHandoff, _digest, _scheduled_at
from app.modules.ai_workbench.models import BuildOccurrence
from app.modules.ai_workbench.plans import AIDailyPlanRow


def _row(**values: object) -> AIDailyPlanRow:
    defaults = {
        "tenant_id": uuid4(),
        "plan_id": uuid4(),
        "optimizer_id": uuid4(),
        "business_date": date(2026, 11, 12),
        "scheduled_time": time(9, 30),
        "title": "剧目",
        "provider_connection_id": uuid4(),
        "application_id": "app-1",
        "promotion_url": None,
        "external_drama_id": "0012",
        "order_materials": True,
        "position": 0,
    }
    defaults.update(values)
    return AIDailyPlanRow(**defaults)


def test_scheduled_occurrence_is_timezone_aware_and_preserves_leading_zero_id() -> None:
    row = _row()
    assert _scheduled_at(row) == datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
    assert row.external_drama_id == "0012"


def test_occurrence_digest_is_stable_for_frozen_snapshot() -> None:
    payload = {"optimizer_id": str(uuid4()), "advertiser_ids": ["a-1"], "strategy": "s-1"}
    assert _digest(payload) == _digest(dict(payload))
    assert _digest({**payload, "advertiser_ids": ["a-2"]}) != _digest(payload)


def test_occurrence_status_contract_and_frozen_scope() -> None:
    tenant_id, optimizer_id, row_id, strategy_id = uuid4(), uuid4(), uuid4(), uuid4()
    occurrence = BuildOccurrence(
        tenant_id=tenant_id,
        optimizer_id=optimizer_id,
        plan_row_id=row_id,
        request_id=uuid4(),
        request_digest="a" * 64,
        strategy_version_id=strategy_id,
        bc_id="bc-1",
        advertiser_ids=["adv-1", "adv-2"],
        provider_connection_id=uuid4(),
        application_id="app-1",
        title="剧目",
        scheduled_at=datetime(2026, 11, 12, tzinfo=UTC),
    )
    assert occurrence.status == "PENDING"
    assert occurrence.advertiser_ids == ["adv-1", "adv-2"]
    assert occurrence.optimizer_id == optimizer_id
    assert occurrence.strategy_version_id == strategy_id


def test_unknown_occurrence_is_never_re_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    occurrence = BuildOccurrence(
        tenant_id=uuid4(),
        optimizer_id=uuid4(),
        plan_row_id=uuid4(),
        request_id=uuid4(),
        request_digest="b" * 64,
        strategy_version_id=uuid4(),
        bc_id="bc-1",
        advertiser_ids=["adv-1"],
        provider_connection_id=uuid4(),
        application_id="app-1",
        title="剧目",
        scheduled_at=datetime.now(UTC),
        status="UNKNOWN",
    )

    class Result:
        def one_or_none(self) -> BuildOccurrence:
            return occurrence

    class Session:
        def exec(self, _query: object) -> Result:
            return Result()

        def add(self, _row: object) -> None:
            raise AssertionError("UNKNOWN occurrence must not be mutated")

        def flush(self) -> None:
            raise AssertionError("UNKNOWN occurrence must not be flushed")

    monkeypatch.setattr(BuildHandoff, "_authorize", lambda self: None)
    result = BuildHandoff(Session(), context=type("Context", (), {"tenant_id": occurrence.tenant_id, "actor_id": uuid4()})()).dispatch_occurrence(occurrence.id)
    assert result.status == "UNKNOWN"

