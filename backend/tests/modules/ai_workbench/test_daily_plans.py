"""Daily plan contract tests; all cases stay local and never call TikTok."""

from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlmodel import select

from app.modules.ai_workbench.models import AIOptimizerStrategyBinding
from app.modules.ai_workbench.plan_schemas import (
    DailyPlanRequest,
    DailyPlanRowInput,
    parse_plan_tsv,
    validate_plan_rows,
)
from app.modules.providers.models import ProviderApplication, ProviderConnection


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


def _provider_application(session, context, *, application_id="app-a"):
    connection = ProviderConnection(
        tenant_id=context.tenant_id,
        kind="jiashu",
        display_name="测试版权方",
        status="active",
    )
    session.add(connection)
    session.flush()
    application = ProviderApplication(
        tenant_id=context.tenant_id,
        connection_id=connection.id,
        external_id=application_id,
        name="测试小程序",
    )
    session.add(application)
    session.flush()
    return connection, application


def _plan_body(optimizer_id, connection_id, application_id="app-a", **overrides):
    value = row(
        optimizer_id=str(optimizer_id),
        provider_connection_id=str(connection_id),
        application_id=application_id,
    )
    value.update(overrides)
    return {"business_date": "2026-11-12", "rows": [value]}


def test_daily_plan_api_persists_order_intent_and_checks_provider_scope(
    client, session, context
):
    from .test_optimizer import api_headers, create_body, seed_optimizer_dependencies

    strategy = seed_optimizer_dependencies(session, context)
    optimizer = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context),
        json=create_body(strategy.id),
    ).json()
    connection, _application = _provider_application(session, context)
    response = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers/{optimizer['id']}/daily-plans",
        headers=api_headers(context),
        json=_plan_body(optimizer["id"], connection.id, external_drama_id="0012"),
    )
    assert response.status_code == 201, response.text
    assert response.json()["rows"][0]["external_drama_id"] == "0012"
    assert response.json()["rows"][0]["order_materials"] is True
    wrong_scope = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers/{optimizer['id']}/daily-plans",
        headers=api_headers(context),
        json=_plan_body(optimizer["id"], uuid4()),
    )
    assert wrong_scope.status_code == 422


def test_daily_plan_rejects_inactive_optimizer_and_missing_active_binding(
    client, session, context
):
    from .test_optimizer import api_headers, create_body, seed_optimizer_dependencies

    strategy = seed_optimizer_dependencies(session, context)
    connection, _application = _provider_application(session, context)
    inactive = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context),
        json=create_body(strategy.id, active=False),
    ).json()
    inactive_response = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers/{inactive['id']}/daily-plans",
        headers=api_headers(context),
        json=_plan_body(inactive["id"], connection.id),
    )
    assert inactive_response.status_code == 409
    strategy_two = seed_optimizer_dependencies(session, context, account="acc-2")
    active = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context),
        json=create_body(strategy_two.id, advertiser_ids=["acc-2"]),
    ).json()
    binding = session.exec(
        select(AIOptimizerStrategyBinding).where(
            AIOptimizerStrategyBinding.optimizer_id == active["id"]
        )
    ).one()
    binding.active = False
    session.flush()
    missing_binding = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers/{active['id']}/daily-plans",
        headers=api_headers(context),
        json=_plan_body(active["id"], connection.id),
    )
    assert missing_binding.status_code == 409
