"""历史连接展示使用已保存路线，不受今天的默认或可用性影响。"""

import pytest

from app.core.errors import DomainError
from app.modules.accounts.connection_models import BCConnectionBinding, BCDefaultRoute
from app.modules.accounts.models import TikTokConnection
from app.modules.builds.route_views import execution_route_view
from tests.modules.builds.test_drafts import account
from tests.modules.builds.test_route_migration import historical_rows
from tests.modules.conftest import create_context


def test_route_projection_keeps_saved_connection_after_disable_and_default_change(
    session, context
):
    _, preview, _ = historical_rows(session, context=context, current=True)
    original = execution_route_view(session, context=context, preview_id=preview.id)
    assert original is not None
    connection = session.get(TikTokConnection, original.connection_id)
    connection.display_name = "原搭建连接"
    connection.status = "DISABLED"
    connection.authorization_revision += 1
    default = session.get(BCDefaultRoute, (context.tenant_id, preview.bc_id))
    replacement = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(replacement)
    session.flush()
    session.add(
        BCConnectionBinding(
            tenant_id=context.tenant_id,
            bc_id=preview.bc_id,
            connection_id=replacement.id,
            kind="OFFICIAL_API",
        )
    )
    session.flush()
    default.connection_id = replacement.id
    session.flush()

    view = execution_route_view(session, context=context, preview_id=preview.id)
    assert view.connection_id == original.connection_id
    assert view.connection_name == "原搭建连接"
    assert view.channel == "OFFICIAL_API"
    assert view.bc_id == preview.bc_id
    assert set(view.model_dump()) == {
        "connection_id",
        "connection_name",
        "channel",
        "bc_id",
    }


def test_missing_historical_route_does_not_infer_new_default(session, context):
    _, preview, _ = historical_rows(session, context=context, connections=0)
    account(session, context)
    assert session.get(BCDefaultRoute, (context.tenant_id, preview.bc_id)) is not None
    assert execution_route_view(session, context=context, preview_id=preview.id) is None


def test_route_projection_cannot_read_another_tenants_preview(session, context):
    _, preview, _ = historical_rows(session, context=context, current=True)
    other = create_context(session)
    with pytest.raises(DomainError) as error:
        execution_route_view(session, context=other, preview_id=preview.id)
    assert error.value.code == "preview_not_found"


def test_preview_and_submission_http_return_only_safe_frozen_route(
    client, session, context, other_context
):
    from tests.modules.strategies.test_api import headers
    from tests.modules.strategies.test_versions import config

    _, preview, step = historical_rows(
        session,
        context=context,
        current=True,
        preview_config=config().model_dump(mode="json"),
    )
    for suffix in (f"build-previews/{preview.id}", f"submissions/{step.submission_id}"):
        response = client.get(
            f"/api/tenants/{context.tenant_id}/{suffix}", headers=headers(context)
        )
        assert response.status_code == 200, response.json()
        route = response.json()["execution_route"]
        assert set(route) == {"connection_id", "connection_name", "channel", "bc_id"}
        assert route["bc_id"] == preview.bc_id and route["channel"] == "OFFICIAL_API"
        assert (
            client.get(
                f"/api/tenants/{other_context.tenant_id}/{suffix}",
                headers=headers(other_context),
            ).status_code
            == 404
        )
