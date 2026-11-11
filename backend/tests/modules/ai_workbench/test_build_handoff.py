from datetime import UTC, date, datetime, time
from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.modules.ai_workbench.build_handoff import (
    MATERIAL_WAIT_ERRORS,
    BuildHandoff,
    _digest,
    _scheduled_at,
)
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

        def get(self, _model: object, _identity: object) -> object:
            return type("Submission", (), {"status": "COMPLETED"})()

        def add(self, _row: object) -> None:
            raise AssertionError("UNKNOWN occurrence must not be mutated")

        def flush(self) -> None:
            raise AssertionError("UNKNOWN occurrence must not be flushed")

    monkeypatch.setattr(BuildHandoff, "_authorize", lambda self: None)
    result = BuildHandoff(Session(), context=type("Context", (), {"tenant_id": occurrence.tenant_id, "actor_id": uuid4()})()).dispatch_occurrence(occurrence.id)
    assert result.status == "UNKNOWN"


def test_waiting_material_can_be_rearmed_with_a_new_outbox_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occurrence = BuildOccurrence(
        tenant_id=uuid4(), optimizer_id=uuid4(), plan_row_id=uuid4(), request_id=uuid4(),
        request_digest="c" * 64, strategy_version_id=uuid4(), bc_id="bc-1",
        advertiser_ids=["adv-1"], provider_connection_id=uuid4(), application_id="app-1",
        title="剧目", scheduled_at=datetime.now(UTC), status="WAITING_MATERIAL",
        evidence={"retry_generation": 2},
    )

    class Result:
        def one_or_none(self) -> BuildOccurrence:
            return occurrence

    class Session:
        def exec(self, _query: object) -> Result:
            return Result()

        def get(self, _model: object, _identity: object) -> object:
            return type("Submission", (), {"status": "COMPLETED"})()

        def add(self, _row: object) -> None:
            return None

        def flush(self) -> None:
            return None

    monkeypatch.setattr(BuildHandoff, "_authorize", lambda self: None)
    monkeypatch.setattr(
        "app.modules.ai_workbench.build_handoff.enqueue_after_commit",
        lambda *args, **kwargs: uuid4(),
    )
    result = BuildHandoff(
        Session(), context=type("Context", (), {"tenant_id": occurrence.tenant_id, "actor_id": uuid4()})()
    ).schedule_retry(occurrence.id)
    assert result.status == "PENDING"
    assert result.evidence["retry_generation"] == 3


def test_unknown_cannot_be_marked_dispatched_without_explicit_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occurrence = BuildOccurrence(
        tenant_id=uuid4(), optimizer_id=uuid4(), plan_row_id=uuid4(), request_id=uuid4(),
        request_digest="d" * 64, strategy_version_id=uuid4(), bc_id="bc-1",
        advertiser_ids=["adv-1"], provider_connection_id=uuid4(), application_id="app-1",
        title="剧目", scheduled_at=datetime.now(UTC), status="UNKNOWN",
    )

    class Result:
        def one_or_none(self) -> BuildOccurrence:
            return occurrence

    class Session:
        def exec(self, _query: object) -> Result:
            return Result()

    monkeypatch.setattr(BuildHandoff, "_authorize", lambda self: None)
    with pytest.raises(DomainError) as caught:
        BuildHandoff(
            Session(), context=type("Context", (), {"tenant_id": occurrence.tenant_id, "actor_id": uuid4()})()
        ).reconcile_occurrence(occurrence.id, state="DISPATCHED")
    assert getattr(caught.value, "code", None) == "ai_build_occurrence_retry_required"


def test_result_hook_maps_existing_readback_to_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    submission_id = uuid4()
    occurrence = BuildOccurrence(
        tenant_id=uuid4(), optimizer_id=uuid4(), plan_row_id=uuid4(), request_id=uuid4(),
        request_digest="e" * 64, strategy_version_id=uuid4(), bc_id="bc-1",
        advertiser_ids=["adv-1"], provider_connection_id=uuid4(), application_id="app-1",
        title="剧目", scheduled_at=datetime.now(UTC), status="DISPATCHED", submission_id=submission_id,
    )

    class Result:
        def one_or_none(self) -> BuildOccurrence:
            return occurrence

    class Session:
        def exec(self, _query: object) -> Result:
            return Result()

        def get(self, _model: object, _identity: object) -> object:
            return type("Submission", (), {"status": "COMPLETED"})()

        def add(self, _row: object) -> None:
            return None

        def flush(self) -> None:
            return None

    monkeypatch.setattr(BuildHandoff, "_authorize", lambda self: None)
    result = BuildHandoff(
        Session(), context=type("Context", (), {"tenant_id": occurrence.tenant_id, "actor_id": uuid4()})()
    ).record_submission_result(submission_id, state="SUCCEEDED", evidence={"remote_id": "123"})
    assert result is occurrence
    assert occurrence.status == "COMPLETE"
    assert occurrence.evidence["readback"]["remote_id"] == "123"


def test_material_readiness_errors_are_resumable() -> None:
    assert {"material_preparing", "material_unavailable"}.issubset(MATERIAL_WAIT_ERRORS)


def test_nonterminal_submission_does_not_terminalize_multi_step_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submission_id = uuid4()
    occurrence = BuildOccurrence(
        tenant_id=uuid4(), optimizer_id=uuid4(), plan_row_id=uuid4(), request_id=uuid4(),
        request_digest="f" * 64, strategy_version_id=uuid4(), bc_id="bc-1",
        advertiser_ids=["adv-1"], provider_connection_id=uuid4(), application_id="app-1",
        title="剧目", scheduled_at=datetime.now(UTC), status="DISPATCHED", submission_id=submission_id,
    )

    class Result:
        def one_or_none(self) -> BuildOccurrence:
            return occurrence

    class Session:
        def exec(self, _query: object) -> Result:
            return Result()

        def get(self, _model: object, _identity: object) -> object:
            return type("Submission", (), {"status": "RUNNING"})()

    monkeypatch.setattr(BuildHandoff, "_authorize", lambda self: None)
    result = BuildHandoff(
        Session(), context=type("Context", (), {"tenant_id": occurrence.tenant_id, "actor_id": uuid4()})()
    ).record_submission_result(submission_id, state="SUCCEEDED")
    assert result is None
    assert occurrence.status == "DISPATCHED"


def test_needs_review_aggregate_maps_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    submission_id = uuid4()
    occurrence = BuildOccurrence(
        tenant_id=uuid4(), optimizer_id=uuid4(), plan_row_id=uuid4(), request_id=uuid4(),
        request_digest="g" * 64, strategy_version_id=uuid4(), bc_id="bc-1",
        advertiser_ids=["adv-1"], provider_connection_id=uuid4(), application_id="app-1",
        title="剧目", scheduled_at=datetime.now(UTC), status="DISPATCHED", submission_id=submission_id,
    )

    class Result:
        def one_or_none(self) -> BuildOccurrence:
            return occurrence

    class Session:
        def exec(self, _query: object) -> Result:
            return Result()

        def get(self, _model: object, _identity: object) -> object:
            return type("Submission", (), {"status": "NEEDS_REVIEW"})()

        def add(self, _row: object) -> None:
            return None

        def flush(self) -> None:
            return None

    monkeypatch.setattr(BuildHandoff, "_authorize", lambda self: None)
    result = BuildHandoff(
        Session(), context=type("Context", (), {"tenant_id": occurrence.tenant_id, "actor_id": uuid4()})()
    ).record_submission_result(submission_id, state="UNKNOWN")
    assert result is occurrence
    assert occurrence.status == "UNKNOWN"

