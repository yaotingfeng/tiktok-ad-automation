"""Small real-PG diagnostic probes; no task graph or remote calls are needed."""

import ast
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from app.jobs.models import PendingDispatch
from app.modules.builds import drafts
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial, MaterialDistribution


def test_scoped_diagnostics_are_readonly_and_preserve_other_queue(acceptance_scenario):
    scenario = acceptance_scenario
    runtime = scenario.runtime
    now = datetime.now(UTC)
    with Session(scenario.database_engine) as session, session.begin():
        for index, scope in enumerate((scenario.scope, scenario.other)):
            draft_id = drafts.create_draft(
                session,
                context=scope.context,
                bc_id=scope.bc_id,
                strategy_version_id=scope.version_id,
                provider_connection_id=scope.provider_id,
                application_id=scope.application_id,
                drama_lines=["The Bond"],
                account_lines=list(scope.accounts),
                link_config={"episode": 1},
            )
            drafts.prepare_draft(
                session,
                context=scope.context,
                draft_id=draft_id,
                request_id=uuid4(),
            )
            asset = session.exec(
                select(AccountMaterial).where(
                    AccountMaterial.tenant_id == scope.context.tenant_id
                )
            ).first()
            assert asset
            session.add(
                MaterialDistribution(
                    tenant_id=scope.context.tenant_id,
                    bc_id=scope.bc_id,
                    material_id=asset.material_id,
                    advertiser_id=asset.advertiser_id,
                    actor_id=scope.context.actor_id,
                    path="upload_original",
                    status="queued" if index == 0 else "blocked",
                )
            )
            session.add(
                MaterialCoverJob(
                    tenant_id=scope.context.tenant_id,
                    bc_id=scope.bc_id,
                    material_id=asset.material_id,
                    asset_id=asset.id,
                    advertiser_id=asset.advertiser_id,
                    connection_id=scope.connection_id,
                    actor_id=scope.context.actor_id,
                    video_id=asset.video_id,
                    remote_name="not-to-be-printed.jpg",
                    status="PENDING" if index == 0 else "BLOCKED",
                )
            )
        session.add(
            PendingDispatch(
                tenant_id=scenario.scope.context.tenant_id,
                actor_id=scenario.scope.context.actor_id,
                task_name="jobs.published_probe",
                task_key="already-published",
                payload={},
                available_at=now - timedelta(seconds=30),
                published_at=now,
            )
        )
        pending = session.exec(select(PendingDispatch)).all()
        for row in pending:
            if row.published_at is None:
                row.available_at = now + timedelta(seconds=40)
        session.add(
            PendingDispatch(
                tenant_id=scenario.other.context.tenant_id,
                actor_id=scenario.other.context.actor_id,
                task_name="jobs.other_probe",
                task_key="not-to-be-printed",
                payload={"token": "not-to-be-printed"},
                available_at=now - timedelta(seconds=10),
            )
        )
    for tenant in (scenario.scope.context.tenant_id, scenario.other.context.tenant_id):
        runtime.publish(
            "jobs.probe",
            task_id=str(uuid4()),
            kwargs={"tenant_id": str(tenant), "token": "not-to-be-printed"},
        )
    runtime.publish("jobs.probe", task_id=str(uuid4()), kwargs={})
    original_messages = list(runtime.messages)
    statements = []

    def observe(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(scenario.database_engine, "before_cursor_execute", observe)
    try:
        diagnostic = runtime.diagnostics(tenant_id=scenario.scope.context.tenant_id)
    finally:
        event.remove(scenario.database_engine, "before_cursor_execute", observe)
    scoped = ast.literal_eval(diagnostic)
    assert len(scoped["drafts"]) == 1
    assert scoped["materials"] == {("queued", None): 1}
    assert scoped["covers"] == {("PENDING", None): 1}
    assert "jobs.other_probe" not in scoped["unpublished_dispatches"]
    assert "jobs.published_probe" not in scoped["unpublished_dispatches"]
    assert 30 <= scoped["nearest_due_seconds"] <= 40
    assert scoped["queued_messages"] == {
        "total": 3,
        "matching_tenant": 1,
        "other_tenants": 1,
        "unscoped": 1,
    }
    assert all(statement.lstrip().startswith("SELECT") for statement in statements)
    for table in (
        "draft_preparation",
        "execution_step",
        "material_cover_job",
        "material_distribution",
        "pending_dispatch",
    ):
        queries = [
            statement for statement in statements if f"FROM {table}" in statement
        ]
        assert queries and all(f"{table}.tenant_id =" in query for query in queries)
    assert "not-to-be-printed" not in diagnostic
    assert str(scenario.scope.context.tenant_id) not in diagnostic
    assert str(scenario.other.context.tenant_id) not in diagnostic
    assert list(runtime.messages) == original_messages and not runtime.delivered
    assert not runtime.wire.calls
    global_diagnostic = ast.literal_eval(runtime.diagnostics())
    assert len(global_diagnostic["drafts"]) == 2
    assert global_diagnostic["unpublished_dispatches"]["jobs.other_probe"] == 1
    assert global_diagnostic["nearest_due_seconds"] == 0
    assert global_diagnostic["materials"] == {("queued", None): 1, ("blocked", None): 1}
    missing = ast.literal_eval(runtime.diagnostics(tenant_id=uuid4()))
    assert not missing["drafts"] and not missing["unpublished_dispatches"]
    assert missing["nearest_due_seconds"] is None


def diagnostic_server(monkeypatch, tmp_path):
    # Import the actual test-server routes onto a separate app: production app
    # globals, running browser servers, lifespan and real jobs remain untouched.
    import app.main

    application = FastAPI()
    (tmp_path / "index.html").write_text("offline")
    monkeypatch.setattr(app.main, "app", application)
    monkeypatch.setattr(app.main, "FRONTEND_DIR", tmp_path)
    spec = importlib.util.spec_from_file_location(
        "acceptance_diagnostics_server", Path(__file__).with_name("browser_server.py")
    )
    assert spec and spec.loader
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server, application


def test_pump_optional_scope_is_validated_before_any_delivery(monkeypatch, tmp_path):
    server, application = diagnostic_server(monkeypatch, tmp_path)
    tenant = uuid4()
    server.scopes[str(tenant)] = object()
    calls = []
    server.runtime = SimpleNamespace(
        pump_jobs=lambda: calls.append("pump") or 0,
        diagnostics=lambda **kwargs: calls.append(kwargs) or "safe diagnostics",
    )
    client = TestClient(application)
    for payload in (None, {}, {"tenant_id": str(tenant)}):
        response = client.post("/__acceptance__/pump", json=payload)
        assert response.status_code == 200
        assert response.json() == {"delivered": 0, "diagnostics": "safe diagnostics"}
    assert calls == [
        "pump",
        {"tenant_id": None},
        "pump",
        {"tenant_id": None},
        "pump",
        {"tenant_id": tenant},
    ]
    before = list(calls)
    for payload, expected in (
        ({"tenant_id": str(uuid4())}, 404),
        ({"tenant_id": "bad"}, 422),
    ):
        assert client.post("/__acceptance__/pump", json=payload).status_code == expected
    assert calls == before


def test_smart_post_evidence_excludes_another_scenarios_background_work(
    acceptance_scenario, monkeypatch, tmp_path
):
    scenario = acceptance_scenario
    server, application = diagnostic_server(monkeypatch, tmp_path)
    server.engine = scenario.database_engine
    server.wire = scenario.runtime.wire
    for scope in (scenario.scope, scenario.other):
        server.scopes[str(scope.context.tenant_id)] = scope
        server.wire.smart.calls.append(
            {"method": "POST", "body": {"advertiser_id": scope.accounts[0]}}
        )
    server.wire.smart.calls.append(
        {"method": "GET", "body": {"advertiser_id": scenario.scope.accounts[0]}}
    )
    # An accidental advertisement in this tenant's material source account is
    # still a real POST and must not disappear from a no-write assertion.
    server.wire.smart.calls.append(
        {"method": "POST", "body": {"advertiser_id": scenario.scope.sources[0]}}
    )
    client = TestClient(application)
    for scope, expected in ((scenario.scope, 2), (scenario.other, 1)):
        response = client.get(f"/__acceptance__/evidence/{scope.context.tenant_id}")
        assert response.status_code == 200
        assert response.json()["smart_posts"] == expected
        assert response.json()["submission_count"] == 0
