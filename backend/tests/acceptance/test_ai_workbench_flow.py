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
