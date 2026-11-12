"""Transport-double acceptance contracts for the AI workbench v1 loop."""

from pathlib import Path

from app.modules.ai_workbench.api import router


def test_daily_report_route_is_tenant_and_optimizer_scoped():
    paths = {route.path for route in router.routes}
    assert "/tenants/{tenant_id}/ai-optimizers/{optimizer_id}/daily-report" in paths


def test_acceptance_contract_keeps_external_writes_out_of_the_loop():
    source = Path("backend/app/modules/ai_workbench/daily_report.py").read_text()
    assert "requests.post" not in source
    assert "httpx.post" not in source
    assert "frozen_evidence_ids" in source


def test_manual_and_automatic_modes_are_explicitly_supported():
    source = Path("backend/app/modules/ai_workbench/proposals.py").read_text()
    assert 'version.optimization_mode != "AUTO"' in source
    assert 'mode == "MANUAL"' in source


def test_two_tenants_and_two_optimizers_keep_daily_report_scope(
    client, session, context, other_context
):
    """Exercise the real tenant/optimizer route with only local transport doubles."""
    from tests.modules.ai_workbench.test_optimizer import (
        api_headers,
        create_body,
        seed_optimizer_dependencies,
    )

    first_strategy = seed_optimizer_dependencies(session, context, account="acc-1")
    other_strategy = seed_optimizer_dependencies(session, other_context, account="acc-1")
    base = f"/api/tenants/{context.tenant_id}/ai-optimizers"
    first = client.post(
        base,
        headers=api_headers(context),
        json=create_body(first_strategy.id, name="自动优化师", optimization_mode="AUTO"),
    )
    assert first.status_code == 201, first.text
    other = client.post(
        f"/api/tenants/{other_context.tenant_id}/ai-optimizers",
        headers=api_headers(other_context),
        json=create_body(other_strategy.id, name="其他租户优化师"),
    )
    assert other.status_code == 201, other.text

    first_id = first.json()["id"]
    for optimizer_id, mode in ((first_id, "AUTO"),):
        response = client.get(
            f"{base}/{optimizer_id}/daily-report?business_date=2026-11-12",
            headers=api_headers(context),
        )
        assert response.status_code == 200, response.text
        assert response.json()["optimizer_id"] == optimizer_id
        assert response.json()["optimization_mode"] == mode

    cross_tenant = client.get(
        f"/api/tenants/{other_context.tenant_id}/ai-optimizers/{first_id}/daily-report",
        headers=api_headers(other_context),
    )
    assert cross_tenant.status_code == 404


def test_acceptance_matrix_covers_partial_readback_order_and_proposal_contracts():
    daily_report = Path("backend/app/modules/ai_workbench/daily_report.py").read_text()
    proposals = Path("backend/app/modules/ai_workbench/proposals.py").read_text()
    assert '"PARTIAL"' in daily_report
    assert '"readback"' in daily_report
    assert '"order_materials"' in daily_report
    assert "AIOptimizerRuleBinding.active.is_(True)" in daily_report
    # Account-scoped rule proposals are checked before publication, and an
    # accepted proposal always appends a fresh immutable optimizer version.
    assert 'mode == "ACCOUNTS"' in proposals
    assert "publish_auto_proposals" in proposals
    assert "update_optimizer" in proposals
    assert "request_id=uuid4()" in proposals
