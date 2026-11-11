"""Daily plan contract tests; all cases stay local and never call TikTok."""

from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.ai_workbench.plan_schemas import (
    DailyPlanRequest,
    DailyPlanRowInput,
    parse_plan_tsv,
    validate_plan_rows,
)


def row(**overrides) -> dict:
    value = {
        "business_date": "2026-11-12",
        "scheduled_time": "08:30",
        "title": "剧集 A",
        "provider_connection_id": str(uuid4()),
        "application_id": "app-a",
        "external_drama_id": "0012",
        "order_materials": True,
        "optimizer_id": str(uuid4()),
    }
    value.update(overrides)
    return value


def test_tsv_retains_leading_zero_drama_ids_and_order_intent():
    optimizer = uuid4()
    connection = uuid4()
    value = f"2026-11-12\t08:30\t剧集 A\t{connection}\tapp-a\t\t0012\ttrue\t{optimizer}"
    parsed = parse_plan_tsv(value)
    assert parsed[0].external_drama_id == "0012"
    assert parsed[0].order_materials is True


def test_duplicate_titles_are_rejected_only_within_same_day():
    optimizer = uuid4()
    first = DailyPlanRowInput.model_validate(row(optimizer_id=optimizer))
    with pytest.raises(ValueError, match="unique"):
        validate_plan_rows([first, first.model_copy()])
    other_day = first.model_copy(update={"business_date": date(2026, 11, 13)})
    assert len(validate_plan_rows([first, other_day])) == 2


def test_optimizer_scope_is_single_and_material_fields_are_forbidden():
    with pytest.raises(ValidationError):
        DailyPlanRowInput.model_validate(row(material_id=str(uuid4())))
    first = DailyPlanRowInput.model_validate(row())
    second = DailyPlanRowInput.model_validate(row(title="剧集 B", optimizer_id=uuid4()))
    with pytest.raises(ValueError, match="one optimizer"):
        validate_plan_rows([first, second])


def test_request_accepts_tsv_or_rows_but_not_both():
    with pytest.raises(ValidationError):
        DailyPlanRequest.model_validate({"rows": [row()], "tsv": ""})
    body = DailyPlanRequest.model_validate({"rows": [row()]})
    assert body.rows and body.rows[0].external_drama_id == "0012"

