from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import DispatchTenantCursor, PendingDispatch
from app.modules.providers.models import LinkPreparation, LinkPreparationItem
from app.modules.providers.service import (
    choose_drama_candidate,
    clean_lines,
    prepare_links,
)
from tests.modules.providers.test_link_recovery import (
    unit,
)
from tests.modules.providers.test_link_recovery import (
    workflow as workflow,
)


@pytest.fixture
def request_fixture(workflow):
    yield workflow
    with Session(engine) as session:
        session.exec(
            delete(PendingDispatch).where(
                PendingDispatch.tenant_id == workflow[0].tenant_id
            )
        )
        session.exec(
            delete(DispatchTenantCursor).where(
                DispatchTenantCursor.tenant_id == workflow[0].tenant_id
            )
        )
        session.commit()


def prepare(workflow, *, request_id=None, lines=None, commit=True):
    with Session(engine) as session:
        task_id = prepare_links(
            session,
            context=workflow[0],
            connection_id=workflow[1],
            application_id="external-app",
            lines=lines or ["Moon"],
            config={"episode": 1},
            request_id=request_id or uuid4(),
        )
        if commit:
            session.commit()
        return task_id


def test_clean_lines_preserves_line_numbers_exact_case_and_deduplicates():
    assert clean_lines([" Moon ", "", "Moon", "Sun"]) == [(1, "Moon"), (4, "Sun")]
    assert clean_lines(["Moon", "moon"]) == [(1, "Moon"), (2, "moon")]


def test_same_request_is_enqueued_once_and_conflicting_input_rejected(request_fixture):
    request_id = uuid4()
    first = prepare(request_fixture, request_id=request_id)
    assert prepare(request_fixture, request_id=request_id) == first
    with pytest.raises(DomainError) as error:
        prepare(request_fixture, request_id=request_id, lines=["Sun"])
    assert error.value.code == "request_id_conflict"
    with Session(engine) as session:
        rows = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == request_fixture[0].tenant_id,
            )
        ).all()
        assert len(rows) == 1
        assert set(rows[0].payload) <= {"item_id", "revision"}


def test_transaction_rollback_creates_no_work_or_dispatch(request_fixture):
    task_id = prepare(request_fixture, commit=False)
    with Session(engine) as session:
        assert session.get(LinkPreparation, task_id) is None
        assert not session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == request_fixture[0].tenant_id,
            )
        ).all()


def test_concurrent_same_request_has_one_preparation_and_one_dispatch(request_fixture):
    request_id, barrier = uuid4(), Barrier(2)

    def submit(_):
        barrier.wait(timeout=5)
        return prepare(request_fixture, request_id=request_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results[0] == results[1]
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == request_fixture[0].tenant_id,
                    )
                ).all()
            )
            == 1
        )


def test_candidate_must_be_from_same_input_and_resume_exact_choice(request_fixture):
    request_fixture[2].drama_rows.append(
        {"video_id": "102", "name": "Moon", "language": "es"}
    )
    task_id = prepare(request_fixture)
    with Session(engine) as session:
        item_id = session.exec(
            select(LinkPreparationItem.id).where(
                LinkPreparationItem.preparation_id == task_id,
            )
        ).one()
    assert unit(request_fixture, item_id)[0] == "needs_resolution"
    with Session(engine) as session:
        with pytest.raises(DomainError) as error:
            choose_drama_candidate(
                session,
                context=request_fixture[0],
                input_id=item_id,
                external_drama_id="other-app-id",
            )
        assert error.value.code == "candidate_not_available"
        session.rollback()
    with Session(engine) as session:
        assert (
            choose_drama_candidate(
                session,
                context=request_fixture[0],
                input_id=item_id,
                external_drama_id="101",
            )
            == task_id
        )
        session.commit()
    with Session(engine) as session:
        item = session.get(LinkPreparationItem, item_id)
        assert item.status == "pending" and item.resolved["external_drama_id"] == "101"
        assert item.resolved["_work"]["stage"] == "lookup"


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


def auth_headers(context):
    from datetime import timedelta

    from app.core.security import create_access_token

    return {
        "Authorization": "Bearer "
        + create_access_token(context.actor_id, timedelta(minutes=5))
    }


def test_api_preparation_returns_safe_paged_results_and_conflict(
    api_client, request_fixture
):
    context, connection_id, _ = request_fixture
    path = f"/api/tenants/{context.tenant_id}/providers/link-preparations"
    body = {
        "request_id": str(uuid4()),
        "connection_id": str(connection_id),
        "application_id": "external-app",
        "lines": ["Moon", "", "Sun"],
        "config": {"episode": 1},
    }
    first = api_client.post(path, json=body, headers=auth_headers(context))
    assert first.status_code == 202
    assert (
        api_client.post(path, json=body, headers=auth_headers(context)).json()
        == first.json()
    )
    conflict = api_client.post(
        path, json={**body, "lines": ["Other"]}, headers=auth_headers(context)
    )
    assert (
        conflict.status_code == 409 and conflict.json()["code"] == "request_id_conflict"
    )
    result = api_client.get(
        f"{path}/{first.json()['task_id']}",
        headers=auth_headers(context),
        params={"page_size": 50},
    )
    assert result.status_code == 200
    assert [row["line_no"] for row in result.json()["items"]] == [1, 3]
    assert "_work" not in result.text and "fake-private-session" not in result.text
    assert (
        api_client.get(
            f"{path}/{first.json()['task_id']}",
            headers=auth_headers(context),
            params={"page_size": 10},
        ).status_code
        == 422
    )


def test_api_foreign_tenant_cannot_read_preparation_or_use_connection(
    api_client, request_fixture
):
    from app.models import User
    from app.modules.tenants.models import Tenant, TenantMembership
    from tests.modules.conftest import create_context

    task_id = prepare(request_fixture)
    with Session(engine) as session:
        foreign = create_context(session)
        session.commit()
    try:
        base = f"/api/tenants/{foreign.tenant_id}/providers"
        result = api_client.get(
            f"{base}/link-preparations/{task_id}", headers=auth_headers(foreign)
        )
        assert result.status_code == 404
        response = api_client.post(
            f"{base}/link-preparations",
            headers=auth_headers(foreign),
            json={
                "request_id": str(uuid4()),
                "connection_id": str(request_fixture[1]),
                "application_id": "external-app",
                "lines": ["Moon"],
                "config": {},
            },
        )
        assert response.status_code == 404
        assert (
            api_client.get(
                f"{base}/connections/{request_fixture[1]}/applications",
                headers=auth_headers(foreign),
            ).status_code
            == 404
        )
    finally:
        with Session(engine) as session:
            session.exec(
                delete(TenantMembership).where(
                    TenantMembership.tenant_id == foreign.tenant_id
                )
            )
            session.exec(delete(Tenant).where(Tenant.id == foreign.tenant_id))
            session.exec(delete(User).where(User.id == foreign.actor_id))
            session.commit()


def test_connection_api_hides_credentials_and_rejects_secret_reflection(
    api_client, request_fixture
):
    from app.modules.tenants.models import TenantMembership

    with Session(engine) as session:
        member = session.get(
            TenantMembership,
            (request_fixture[0].tenant_id, request_fixture[0].actor_id),
        )
        member.role = "tenant_admin"
        session.add(member)
        session.commit()
    base = f"/api/tenants/{request_fixture[0].tenant_id}/providers/connections"
    headers = auth_headers(request_fixture[0])
    secret = "test-secret-must-never-be-returned"
    malformed = api_client.post(
        base,
        headers=headers,
        json={
            "kind": "jiashu",
            "display_name": "Fixture",
            "credentials": {"password": {"secret": secret}},
        },
    )
    assert malformed.status_code == 422 and secret not in malformed.text
    created = api_client.post(
        base,
        headers=headers,
        json={
            "kind": "jiashu",
            "display_name": "Fixture",
            "credentials": {"username": "fake", "password": secret},
        },
    )
    assert created.status_code == 201
    assert "credentials" not in created.text and secret not in created.text
    result = api_client.get(base, headers=headers, params={"limit": 1})
    assert result.status_code == 200 and result.json()["next_cursor"] is not None
    second = api_client.get(
        base,
        headers=headers,
        params={"limit": 1, "cursor": result.json()["next_cursor"]},
    )
    assert len(second.json()["items"]) == 1 and second.json()["next_cursor"] is None
    disabled = api_client.patch(
        f"{base}/{created.json()['id']}", headers=headers, json={"status": "disabled"}
    )
    assert disabled.status_code == 200 and disabled.json()["status"] == "disabled"
    invalid_enable = api_client.patch(
        f"{base}/{created.json()['id']}", headers=headers, json={"status": "active"}
    )
    assert invalid_enable.status_code == 422


def test_viewer_cannot_prepare_or_choose_and_operator_cannot_manage(
    api_client, request_fixture
):
    from app.modules.tenants.models import TenantMembership

    base = f"/api/tenants/{request_fixture[0].tenant_id}/providers"
    headers = auth_headers(request_fixture[0])
    assert (
        api_client.post(
            f"{base}/connections",
            headers=headers,
            json={
                "kind": "jiashu",
                "display_name": "No",
                "credentials": {"username": "fake", "password": "fake"},
            },
        ).status_code
        == 403
    )
    with Session(engine) as session:
        member = session.get(
            TenantMembership,
            (request_fixture[0].tenant_id, request_fixture[0].actor_id),
        )
        member.role = "viewer"
        session.add(member)
        session.commit()
    assert (
        api_client.post(
            f"{base}/link-preparations",
            headers=headers,
            json={
                "request_id": str(uuid4()),
                "connection_id": str(request_fixture[1]),
                "application_id": "external-app",
                "lines": ["Moon"],
            },
        ).status_code
        == 403
    )
    assert (
        api_client.post(
            f"{base}/inputs/{uuid4()}/candidate",
            headers=headers,
            json={"external_drama_id": "101"},
        ).status_code
        == 403
    )


def test_applications_are_paged_and_stale_verification_is_visible(
    api_client, request_fixture
):
    from app.modules.providers.models import ProviderApplication

    context, connection_id, _ = request_fixture
    with Session(engine) as session:
        for number in range(100):
            session.add(
                ProviderApplication(
                    tenant_id=context.tenant_id,
                    connection_id=connection_id,
                    external_id=f"stale-{number}",
                    name=f"Stale {number}",
                    channel_config={},
                )
            )
        session.commit()
    path = f"/api/tenants/{context.tenant_id}/providers/connections/{connection_id}/applications"
    first = api_client.get(path, headers=auth_headers(context))
    assert first.status_code == 200 and len(first.json()["items"]) == 50
    all_items, cursor = first.json()["items"], first.json()["next_cursor"]
    while cursor:
        page = api_client.get(
            path, headers=auth_headers(context), params={"cursor": cursor}
        ).json()
        all_items.extend(page["items"])
        cursor = page["next_cursor"]
    assert len(all_items) == 101
    assert [item["external_id"] for item in all_items if item["available"]] == [
        "external-app"
    ]
    assert all("channel_config" not in item and "id" not in item for item in all_items)


def test_verify_api_uses_connection_session_and_returns_only_public_fields(
    api_client, request_fixture, monkeypatch
):
    from app.modules.providers import router as provider_router
    from app.modules.providers.connections import verify_connection
    from app.modules.tenants.models import TenantMembership
    from tests.modules.providers.test_connection_isolation import transport

    with Session(engine) as session:
        member = session.get(
            TenantMembership,
            (request_fixture[0].tenant_id, request_fixture[0].actor_id),
        )
        member.role = "tenant_admin"
        session.add(member)
        session.commit()
    offline = transport()

    def verify_offline(**kwargs):
        return verify_connection(**kwargs, transport=offline)

    monkeypatch.setattr(provider_router, "verify_connection", verify_offline)
    base = f"/api/tenants/{request_fixture[0].tenant_id}/providers/connections"
    headers = auth_headers(request_fixture[0])
    created = api_client.post(
        base,
        headers=headers,
        json={
            "kind": "jiashu",
            "display_name": "Offline verify",
            "credentials": {"username": "fake", "password": "private-password"},
        },
    )
    assert created.status_code == 201
    verified = api_client.post(f"{base}/{created.json()['id']}/verify", headers=headers)
    assert verified.status_code == 200 and verified.json()["status"] == "active"
    assert "private" not in verified.text and "credentials" not in verified.text
    applications = api_client.get(
        f"{base}/{created.json()['id']}/applications", headers=headers
    )
    assert applications.status_code == 200
    assert applications.json()["items"][0]["external_id"] == "external-app"
    assert applications.json()["items"][0]["available"] is True
