def test_reporting_routes_have_stable_operation_ids():
    from app.main import app

    reporting = []
    for _path, methods in app.openapi()["paths"].items():
        for _method, route in methods.items():
            if route.get("tags") == ["ads_reporting"]:
                reporting.append(route)
    assert reporting
    operation_ids = [route["operationId"] for route in reporting]
    assert all(operation_id.startswith("ads_reporting-") for operation_id in operation_ids)
    assert len(operation_ids) == len(set(operation_ids))


def test_query_snapshot_survives_request_session_rollback(report_case, client):
    """GET creates a durable local snapshot consumed by a later request."""
    from app.api.deps import get_current_user
    from app.main import app
    from app.models import User

    user = report_case.session.get(User, report_case.context.actor_id)
    assert user is not None
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        path = f"/api/tenants/{report_case.context.tenant_id}/ads"
        params = {
            "bc_id": report_case.bc_id,
            "dimension": "campaign",
            "start_date": "2026-09-30",
            "end_date": "2026-09-30",
        }
        report_case.seed_campaign("P-API", "report-account", 1, 2)
        first = client.get(path, params=params)
        assert first.status_code == 200, first.text
        snapshot_id = first.json()["snapshot"]["snapshot_id"]
        second = client.get(path, params=params | {"snapshot_id": snapshot_id})
        assert second.status_code == 200, second.text
        assert second.json()["snapshot"]["snapshot_id"] == snapshot_id
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous
