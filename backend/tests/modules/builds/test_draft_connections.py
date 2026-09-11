"""显式草稿连接只影响新准备，旧父路由和幂等身份保持不变。"""

from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.modules.accounts.connection_models import BCConnectionBinding, BCDefaultRoute
from app.modules.accounts.models import TenantBC, TikTokConnection
from app.modules.builds.catalog import draft_summary
from app.modules.builds.drafts import create_draft, prepare_draft, update_draft
from app.modules.builds.models import BuildDraft, DraftPreparation
from app.modules.builds.scene_job_models import DraftScenePreparation
from tests.modules.builds.test_drafts import account


def connection(session, context, *, bc_id="bc-draft"):
    row = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(row)
    session.flush()
    session.add(
        BCConnectionBinding(
            tenant_id=context.tenant_id,
            bc_id=bc_id,
            connection_id=row.id,
            kind=row.kind,
        )
    )
    session.flush()
    return row.id


def test_explicit_connection_persists_and_freezes_without_a_default(
    session, context, intent
):
    selected = connection(session, context)
    draft_id = create_draft(
        session, context=context, execution_connection_id=selected, **intent
    )
    assert (
        draft_summary(
            session, context=context, draft_id=draft_id
        ).execution_connection_id
        == selected
    )
    task = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    prep = session.get(DraftPreparation, task)
    scene = session.get(DraftScenePreparation, (context.tenant_id, task))
    assert prep is not None and scene.frozen_route["connection_id"] == str(selected)


def test_changing_draft_choice_keeps_old_preparation_and_clear_uses_new_default(
    session, context, intent
):
    account(session, context)
    default = session.get(BCDefaultRoute, (context.tenant_id, intent["bc_id"]))
    original = default.connection_id
    selected = connection(session, context)
    draft_id = create_draft(session, context=context, **intent)
    old_task = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    old_route = dict(
        session.get(DraftScenePreparation, (context.tenant_id, old_task)).frozen_route
    )
    revision = update_draft(
        session,
        context=context,
        draft_id=draft_id,
        expected_revision=session.get(BuildDraft, draft_id).revision,
        execution_connection_id=selected,
    )
    new_task = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    assert session.get(
        DraftScenePreparation, (context.tenant_id, new_task)
    ).frozen_route["connection_id"] == str(selected)
    assert (
        session.get(DraftScenePreparation, (context.tenant_id, old_task)).frozen_route
        == old_route
    )
    assert old_route["connection_id"] == str(original)
    update_draft(
        session,
        context=context,
        draft_id=draft_id,
        expected_revision=revision,
        execution_connection_id=None,
    )
    assert (
        draft_summary(
            session, context=context, draft_id=draft_id
        ).execution_connection_id
        is None
    )
    cleared_task = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    assert session.get(
        DraftScenePreparation, (context.tenant_id, cleared_task)
    ).frozen_route["connection_id"] == str(original)


def test_explicit_selection_is_scoped_and_original_create_replay_does_not_rebind(
    session, context, intent
):
    selected = connection(session, context)
    request = uuid4()
    draft_id = create_draft(
        session,
        context=context,
        request_id=request,
        execution_connection_id=selected,
        **intent,
    )
    session.get(TikTokConnection, selected).status = "DISABLED"
    session.flush()
    assert (
        create_draft(
            session,
            context=context,
            request_id=request,
            execution_connection_id=selected,
            **intent,
        )
        == draft_id
    )
    session.add(TenantBC(tenant_id=context.tenant_id, bc_id="different-bc"))
    session.flush()
    wrong = connection(session, context, bc_id="different-bc")
    for choice, code in (
        (wrong, "connection_bc_mismatch"),
        (uuid4(), "connection_not_found"),
    ):
        with pytest.raises(DomainError) as caught:
            create_draft(
                session, context=context, execution_connection_id=choice, **intent
            )
        assert caught.value.code == code


def test_http_draft_choice_can_be_preserved_or_explicitly_cleared(
    client, session, context, intent
):
    from tests.modules.strategies.test_api import headers

    selected = connection(session, context)
    body = {
        key: str(value) if key.endswith("_id") else value
        for key, value in intent.items()
    }
    body |= {"request_id": str(uuid4()), "execution_connection_id": str(selected)}
    base = f"/api/tenants/{context.tenant_id}/build-drafts"
    saved = client.post(base, json=body, headers=headers(context))
    assert saved.status_code == 201
    path = f"{base}/{saved.json()['draft_id']}"
    assert client.get(path, headers=headers(context)).json()[
        "execution_connection_id"
    ] == str(selected)
    changed = client.patch(
        path,
        json={
            "request_id": str(uuid4()),
            "expected_revision": 1,
            "account_lines": ["changed"],
        },
        headers=headers(context),
    )
    assert changed.status_code == 200
    assert client.get(path, headers=headers(context)).json()[
        "execution_connection_id"
    ] == str(selected)
    clear = {
        "request_id": str(uuid4()),
        "expected_revision": changed.json()["revision"],
        "execution_connection_id": None,
    }
    response = client.patch(path, json=clear, headers=headers(context))
    assert response.status_code == 200
    assert (
        client.patch(path, json=clear, headers=headers(context)).json()
        == response.json()
    )
    assert (
        client.get(path, headers=headers(context)).json()["execution_connection_id"]
        is None
    )


def test_new_preview_uses_explicit_choice_and_existing_preview_is_not_refrozen(
    session, context, intent
):
    from app.modules.builds.previews import generate_preview
    from app.modules.builds.routes import load_preview_route

    selected = connection(session, context)
    account(session, context)
    draft_id = create_draft(
        session, context=context, execution_connection_id=selected, **intent
    )
    # 此例只核实已准备草稿创建父预览时的路由，不调用展开或广告接口。
    session.get(BuildDraft, draft_id).status = "READY"
    session.flush()
    preview = generate_preview(
        session, context=context, draft_id=draft_id, expected_revision=1
    )
    route = load_preview_route(session, context=context, preview_id=preview)
    assert route.connection_id == selected
    session.get(TikTokConnection, selected).status = "DISABLED"
    session.flush()
    assert (
        generate_preview(
            session, context=context, draft_id=draft_id, expected_revision=1
        )
        == preview
    )
    assert load_preview_route(session, context=context, preview_id=preview) == route
