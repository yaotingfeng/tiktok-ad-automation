import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlmodel import select

from app.core.config import settings
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.core.security import verify_password
from app.jobs.models import PendingDispatch
from app.models import User
from app.modules.material_production import service
from app.modules.material_production.client import MaterialToolClient
from app.modules.material_production.models import MaterialProductionBinding
from app.modules.tenants.models import AuditEvent, Tenant, TenantMembership
from tests.modules.tenants.test_management import headers, user


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(
        settings, "MATERIAL_TOOL_PROVISIONING_KEYS", {"default": "owp_test-only"}
    )
    monkeypatch.setattr(settings, "MATERIAL_TOOL_DEFAULT_KEY_REF", "default")


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    responses = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request, body))
        if responses:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        if request.url.path.endswith("login-links"):
            return httpx.Response(
                201,
                json={
                    "loginUrl": "/api/login-links/consume?token=short-lived-test-token",
                    "expiresAt": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                },
            )
        return httpx.Response(
            201,
            json={
                "company": {
                    "id": f"company_{body['username']}",
                    "name": body["companyName"],
                    "deliverySettings": {
                        "uploadPlatform": "r2",
                        "pushAdPlatform": True,
                    },
                },
                "user": {
                    "id": f"user_{body['username']}",
                    "username": body["username"],
                },
                "apiKey": {
                    "id": "key_id",
                    "companyId": f"company_{body['username']}",
                    "kind": "company",
                },
                "key": "owk_private-test-value",
                "replayed": len(calls) > 1,
            },
        )

    monkeypatch.setattr(
        service,
        "MaterialToolClient",
        lambda key: MaterialToolClient(key, transport=httpx.MockTransport(handler)),
    )
    return calls, responses


def onboard(session, platform=None):
    platform = platform or user(session, platform=True)
    body = {
        "name": "素材测试租户",
        "username": "material-" + uuid4().hex[:12],
        "password": "Password-123",
        "request_id": uuid4(),
    }
    tenant = service.onboard_tenant(session, actor_id=platform.id, **body)
    return platform, tenant, body


def execute(session, platform, tenant):
    binding = session.get(MaterialProductionBinding, tenant.id)
    service.provision_once(
        session,
        tenant_id=tenant.id,
        actor_id=platform.id,
        sequence=binding.dispatch_sequence,
    )
    return binding


def test_onboarding_is_atomic_encrypted_and_idempotent(session):
    platform, tenant, body = onboard(session)
    binding = session.get(MaterialProductionBinding, tenant.id)
    admin = session.get(User, binding.administrator_id)
    assert verify_password(body["password"], admin.hashed_password)[0]
    assert body["password"] not in binding.request_ciphertext
    assert json.loads(
        decrypt_credentials(tenant_id=tenant.id, ciphertext=binding.request_ciphertext)[
            "request"
        ]
    ) == {
        "companyName": body["name"],
        "username": body["username"],
        "password": body["password"],
        "uploadPlatform": "r2",
        "pushAdPlatform": True,
    }
    assert session.get(TenantMembership, (tenant.id, admin.id)).role == "tenant_admin"
    dispatch = session.exec(
        select(PendingDispatch).where(PendingDispatch.tenant_id == tenant.id)
    ).one()
    assert dispatch.payload == {"binding_id": str(tenant.id), "sequence": 1}
    again = service.onboard_tenant(session, actor_id=platform.id, **body)
    assert again.id == tenant.id
    with pytest.raises(DomainError, match="操作编号"):
        service.onboard_tenant(
            session, actor_id=platform.id, **{**body, "password": "Different-123"}
        )
    assert (
        len(
            session.exec(
                select(PendingDispatch).where(PendingDispatch.tenant_id == tenant.id)
            ).all()
        )
        == 1
    )


def test_success_erases_pending_password_and_never_replays(session, upstream):
    platform, tenant, _ = onboard(session)
    binding = execute(session, platform, tenant)
    assert binding.status == "ready" and binding.request_ciphertext is None
    assert "owk_" not in binding.api_key_ciphertext
    assert decrypt_credentials(
        tenant_id=tenant.id, ciphertext=binding.api_key_ciphertext
    ) == {"api_key": "owk_private-test-value"}
    execute(session, platform, tenant)
    service.retry_provisioning(session, tenant_id=tenant.id, actor_id=platform.id)
    assert len(upstream[0]) == 1


def test_timeout_recovers_exact_body_and_key_without_duplicate_attempts(
    session, upstream
):
    calls, responses = upstream
    responses.append(httpx.ReadTimeout("private response"))
    platform, tenant, _ = onboard(session)
    binding = execute(session, platform, tenant)
    assert binding.status == "pending" and binding.error_code == "UNAVAILABLE"
    execute(session, platform, tenant)
    assert len(calls) == 1
    binding.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    assert service.recover_due(session) == 1
    execute(session, platform, tenant)
    assert binding.status == "ready"
    assert calls[0][0].content == calls[1][0].content
    assert (
        calls[0][0].headers["Idempotency-Key"] == calls[1][0].headers["Idempotency-Key"]
    )


def test_business_error_stops_and_manual_retry_keeps_order(session, upstream):
    upstream[1].append(
        httpx.Response(409, json={"error": "USERNAME_EXISTS", "message": "secret"})
    )
    platform, tenant, _ = onboard(session)
    binding = execute(session, platform, tenant)
    assert binding.status == "failed"
    binding.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    assert service.recover_due(session) == 0
    status = service.production_status(
        session, tenant_id=tenant.id, actor_id=platform.id
    )
    assert "secret" not in status.model_dump_json()
    service.retry_provisioning(session, tenant_id=tenant.id, actor_id=platform.id)
    execute(session, platform, tenant)
    assert binding.status == "ready"
    assert upstream[0][0][0].content == upstream[0][1][0].content


def test_interrupted_dispatch_can_be_recovered_and_old_generation_ignored(
    session, upstream
):
    platform, tenant, _ = onboard(session)
    binding = session.get(MaterialProductionBinding, tenant.id)
    binding.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    assert service.recover_due(session) == 1
    service.provision_once(
        session, tenant_id=tenant.id, actor_id=platform.id, sequence=1
    )
    assert not upstream[0]
    execute(session, platform, tenant)
    assert binding.status == "ready"


def test_rotated_key_and_disabled_tenant_stop_provisioning(
    session, upstream, monkeypatch
):
    platform, tenant, _ = onboard(session)
    monkeypatch.setattr(
        settings, "MATERIAL_TOOL_PROVISIONING_KEYS", {"default": "owp_another-key"}
    )
    binding = execute(session, platform, tenant)
    assert binding.status == "failed" and not upstream[0]


def test_login_scoped_no_store_no_secrets_in_audit(client, session, upstream):
    platform, tenant, body = onboard(session)
    binding = execute(session, platform, tenant)
    path = f"/api/tenants/{tenant.id}/material-production/login-link"
    response = client.post(path, json={}, headers=headers(binding.administrator_id))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["login_url"].startswith(
        "https://manjumt.gzjunbo.net/api/login-links/consume?"
    )
    assert upstream[0][-1][1] == {"username": body["username"]}
    assert upstream[0][-1][0].headers["Authorization"] == "Bearer owp_test-only"
    events = session.exec(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant.id)
    ).all()
    assert all("short-lived-test-token" not in json.dumps(e.details) for e in events)
    assert (
        client.post(
            path, json={"username": "other"}, headers=headers(platform.id)
        ).status_code
        == 422
    )


@pytest.mark.parametrize("role,active", [("viewer", True), ("operator", False)])
def test_login_denies_read_only_or_disabled_members(
    client, session, upstream, role, active
):
    platform, tenant, _ = onboard(session)
    execute(session, platform, tenant)
    actor = user(session)
    session.add(
        TenantMembership(
            tenant_id=tenant.id, user_id=actor.id, role=role, active=active
        )
    )
    session.flush()
    count = len(upstream[0])
    path = f"/api/tenants/{tenant.id}/material-production/login-link"
    assert client.post(path, json={}, headers=headers(actor.id)).status_code == 403
    assert len(upstream[0]) == count


def test_login_denies_other_tenant_and_disabled_tenant(
    client, session, upstream, context
):
    platform, tenant, _ = onboard(session)
    execute(session, platform, tenant)
    path = f"/api/tenants/{tenant.id}/material-production/login-link"
    assert (
        client.post(path, json={}, headers=headers(context.actor_id)).status_code == 403
    )
    tenant.active = False
    session.flush()
    assert client.post(path, json={}, headers=headers(platform.id)).status_code == 403
    assert len(upstream[0]) == 1


def test_create_api_does_not_return_password_or_key(client, session):
    platform = user(session, platform=True)
    body = {
        "name": "中文公司",
        "username": "new-account",
        "password": "Password-123",
        "request_id": str(uuid4()),
    }
    first = client.post(
        "/api/platform/tenants", json=body, headers=headers(platform.id)
    )
    assert first.status_code == 201, first.text
    again = client.post(
        "/api/platform/tenants", json=body, headers=headers(platform.id)
    )
    assert again.json() == first.json()
    assert set(first.json()) == {"id", "name", "active", "default_bc_id"}
    binding = session.get(MaterialProductionBinding, UUID(first.json()["id"]))
    assert binding.status == "pending"


def test_missing_configuration_has_no_local_side_effect(client, session, monkeypatch):
    monkeypatch.setattr(settings, "MATERIAL_TOOL_PROVISIONING_KEYS", {})
    platform = user(session, platform=True)
    body = {
        "name": "未配置",
        "username": "not-created",
        "password": "Password-123",
        "request_id": str(uuid4()),
    }
    response = client.post(
        "/api/platform/tenants", json=body, headers=headers(platform.id)
    )
    assert response.status_code == 422
    assert (
        session.exec(select(User).where(User.username == "not-created")).first() is None
    )
    assert session.exec(select(Tenant).where(Tenant.name == "未配置")).first() is None


def test_five_transient_failures_stop_automatic_retries(session, upstream):
    platform, tenant, _ = onboard(session)
    upstream[1].extend(httpx.Response(503) for _ in range(5))
    for attempt in range(1, 6):
        binding = execute(session, platform, tenant)
        assert binding.attempts == attempt
        assert binding.status == ("failed" if attempt == 5 else "pending")
        binding.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.flush()
        assert service.recover_due(session) == (0 if attempt == 5 else 1)
    assert len(upstream[0]) == 5


def test_parallel_workers_issue_only_one_provision_request(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlmodel import Session, col, delete

    from app.core.db import engine
    from app.jobs.models import DispatchTenantCursor
    from app.jobs.tasks import dispatch_queue
    from app.modules.material_production.tasks import provision_task

    entered, release = Event(), Event()
    calls = []

    def handler(request):
        calls.append(request)
        entered.set()
        assert release.wait(10)
        body = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "company": {
                    "id": "company-concurrent",
                    "name": body["companyName"],
                    "deliverySettings": {
                        "uploadPlatform": "r2",
                        "pushAdPlatform": True,
                    },
                },
                "user": {"id": "user-concurrent", "username": body["username"]},
                "apiKey": {
                    "id": "key-concurrent",
                    "companyId": "company-concurrent",
                    "kind": "company",
                },
                "key": "owk_concurrent-fixture",
            },
        )

    monkeypatch.setattr(
        service,
        "MaterialToolClient",
        lambda key: MaterialToolClient(key, transport=httpx.MockTransport(handler)),
    )
    # 使用两条独立 PostgreSQL 连接，验证真实行锁而非模拟 Session。
    with Session(engine) as db, db.begin():
        platform, tenant, _ = onboard(db)
        actor_id, tenant_id = platform.id, tenant.id
        admin_id = db.get(MaterialProductionBinding, tenant_id).administrator_id

    def run():
        provision_task(
            tenant_id=str(tenant_id),
            actor_id=str(actor_id),
            payload={"binding_id": str(tenant_id), "sequence": 1},
        )

    try:
        assert dispatch_queue("material_production.provision") == "control"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run)
            try:
                assert entered.wait(5)
                pool.submit(run).result(timeout=5)
                assert len(calls) == 1
            finally:
                release.set()
            first.result(timeout=5)
        run()
        assert len(calls) == 1
        with Session(engine) as db:
            assert db.get(MaterialProductionBinding, tenant_id).status == "ready"
    finally:
        with Session(engine) as db, db.begin():
            for model in (
                PendingDispatch,
                DispatchTenantCursor,
                AuditEvent,
                TenantMembership,
                MaterialProductionBinding,
            ):
                db.exec(delete(model).where(model.tenant_id == tenant_id))
            db.exec(delete(Tenant).where(Tenant.id == tenant_id))
            db.exec(delete(User).where(col(User.id).in_([actor_id, admin_id])))


@pytest.fixture
def historical(session):
    from app.core.security import get_password_hash
    from app.modules.tenants.service import create_tenant

    platform, admin = user(session, platform=True), user(session)
    admin.hashed_password = get_password_hash("Existing-password123")
    session.flush()
    tenant = create_tenant(
        session, actor_id=platform.id, name="历史租户", administrator_id=admin.id
    )
    return {
        "actor_id": platform.id,
        "tenant_id": tenant.id,
        "administrator_id": admin.id,
        "expected_name": tenant.name,
        "expected_username": admin.username,
    }


@pytest.mark.parametrize("generated", [False, True])
def test_historical_onboarding_preserves_local_login_and_reuses_binding(
    session, historical, upstream, generated
):
    params = {**historical, "generate_password": generated}
    if not generated:
        params["password"] = "Existing-password123"
    admin = session.get(User, historical["administrator_id"])
    original_hash = admin.hashed_password
    assert service.onboard_existing_tenant(session, **params).status == "pending"
    binding = session.get(MaterialProductionBinding, historical["tenant_id"])
    original_ciphertext = binding.request_ciphertext
    assert service.onboard_existing_tenant(session, **params).status == "pending"
    assert binding.request_ciphertext == original_ciphertext
    service.provision_once(
        session,
        tenant_id=historical["tenant_id"],
        actor_id=historical["actor_id"],
        sequence=1,
    )
    assert service.onboard_existing_tenant(session, **params).status == "ready"
    assert len(upstream[0]) == 1
    request = upstream[0][0][1]
    assert request["companyName"] == historical["expected_name"]
    assert request["username"] == historical["expected_username"]
    assert (request["password"] == "Existing-password123") is not generated
    assert (
        session.get(User, admin.id, populate_existing=True).hashed_password
        == original_hash
    )
    assert session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == historical["tenant_id"]
        )
    ).one().payload == {"binding_id": str(historical["tenant_id"]), "sequence": 1}


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"password": "wrong-password123"},
        {"password": "Existing-password123", "generate_password": True},
        {"expected_name": "错误租户", "generate_password": True},
        {"expected_username": "other", "generate_password": True},
    ],
)
def test_historical_onboarding_rejects_wrong_identity_or_password_mode(
    session, historical, changes
):
    with pytest.raises(DomainError):
        service.onboard_existing_tenant(session, **{**historical, **changes})
    assert session.get(MaterialProductionBinding, historical["tenant_id"]) is None


def test_historical_onboarding_requires_active_tenant_admin(session, historical):
    member = session.get(
        TenantMembership, (historical["tenant_id"], historical["administrator_id"])
    )
    member.active = False
    session.flush()
    with pytest.raises(DomainError):
        service.onboard_existing_tenant(session, **historical, generate_password=True)
    assert session.get(MaterialProductionBinding, historical["tenant_id"]) is None


def test_historical_admin_command_defaults_to_rollback_and_never_prints_password(
    session, historical, monkeypatch, capsys
):
    import io
    import sys

    from sqlmodel import Session

    from app.modules.material_production import admin

    item = {k: str(v) for k, v in historical.items() if k != "actor_id"}
    item["password"] = "Existing-password123"
    body = json.dumps({"actor_id": str(historical["actor_id"]), "tenants": [item]})
    connection = session.connection()
    monkeypatch.setattr(
        admin,
        "Session",
        lambda _: Session(bind=connection, join_transaction_mode="create_savepoint"),
    )
    for apply in (False, True):
        monkeypatch.setattr(sys, "stdin", io.StringIO(body))
        monkeypatch.setattr(
            sys, "argv", ["material-admin", *(["--apply"] if apply else [])]
        )
        assert admin.main() == 0
        output = capsys.readouterr().out
        assert "Existing-password123" not in output
        assert json.loads(output)["applied"] is apply
        binding = session.get(
            MaterialProductionBinding, historical["tenant_id"], populate_existing=True
        )
        assert (binding is not None) is apply
