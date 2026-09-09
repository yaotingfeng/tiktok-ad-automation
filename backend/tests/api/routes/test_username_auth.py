"""Ordinary usernames preserve UUID identity and remove email recovery."""

import pytest
from pydantic import ValidationError

from app.models import UserCreate


def test_username_normalizes_and_has_no_email():
    user = UserCreate(username=" Admin.Name-1_ ", password="Synthetic-password-123")
    assert user.username == "admin.name-1_"
    assert "email" not in user.model_dump()


@pytest.mark.parametrize(
    "value",
    ["a", "ab", "a" * 65, "中文账号", "user@example.com", "user name", "user/one"],
)
def test_username_rejects_invalid_inputs(value):
    with pytest.raises(ValidationError):
        UserCreate(username=value, password="Synthetic-password-123")


def test_mail_routes_removed_from_openapi(client):
    paths = client.get("/api/openapi.json").json()["paths"]
    assert not any(
        "password-recovery" in path or "reset-password" in path or "test-email" in path
        for path in paths
    )


def test_account_login_rename_and_admin_password_reset_preserve_uuid(
    client, db, superuser_token_headers
):
    from app.core.security import verify_password
    from app.models import User

    created = client.post(
        "/api/users/",
        headers=superuser_token_headers,
        json={
            "username": " Team.One ",
            "password": "Original-password-123",
            "full_name": "普通成员",
        },
    )
    assert created.status_code == 200
    identity = created.json()
    assert identity["username"] == "team.one" and "email" not in identity
    from uuid import UUID

    user_id = UUID(identity["id"])
    original_hash = db.get(User, user_id).hashed_password
    login = client.post(
        "/api/login/access-token",
        data={"username": "TEAM.ONE", "password": "Original-password-123"},
    )
    assert login.status_code == 200
    auth = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert (
        client.patch(
            "/api/users/" + identity["id"],
            headers=auth,
            json={"password": "Other-password-123"},
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/api/users/me", headers=auth, json={"email": "legacy@example.com"}
        ).status_code
        == 422
    )
    assert (
        client.patch("/api/users/me", headers=auth, json={"username": None}).status_code
        == 422
    )
    updated = client.patch(
        "/api/users/me", headers=auth, json={"username": " New.Name "}
    )
    assert updated.status_code == 200 and updated.json()["id"] == identity["id"]
    assert updated.json()["username"] == "new.name"
    assert db.get(User, user_id).hashed_password == original_hash
    assert client.get("/api/users/me", headers=auth).json()["id"] == identity["id"]
    reset = client.patch(
        "/api/users/" + identity["id"],
        headers=superuser_token_headers,
        json={"password": "New-password-123"},
    )
    assert reset.status_code == 200
    assert verify_password("New-password-123", db.get(User, user_id).hashed_password)[0]
    assert (
        client.post(
            "/api/login/access-token",
            data={"username": "new.name", "password": "Original-password-123"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/login/access-token",
            data={"username": "NEW.NAME", "password": "New-password-123"},
        ).status_code
        == 200
    )
    duplicate = client.post(
        "/api/users/",
        headers=superuser_token_headers,
        json={"username": "NEW.NAME", "password": "Unique-password-123"},
    )
    assert duplicate.status_code in (400, 409)


@pytest.mark.parametrize(
    "path",
    [
        "/api/password-recovery/legacy@example.com",
        "/api/password-recovery-html-content/legacy@example.com",
        "/api/reset-password/",
        "/api/utils/test-email/",
    ],
)
def test_removed_mail_routes_cannot_send_or_reset(client, path):
    assert (
        client.post(
            path, json={"token": "obsolete", "new_password": "Never-applied-123"}
        ).status_code
        == 404
    )


def test_database_rejects_noncanonical_username(db):
    from uuid import uuid4

    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                'INSERT INTO "user" (id, username, hashed_password, is_active, is_superuser) VALUES (:id, :name, :hash, true, false)'
            ),
            {"id": uuid4(), "name": "UPPER", "hash": "synthetic"},
        )


def test_unique_constraint_race_is_readable_and_does_not_break_session(
    client, superuser_token_headers, monkeypatch
):
    from app import crud

    body = {"username": "raced-account", "password": "Synthetic-password-123"}
    assert (
        client.post(
            "/api/users/", headers=superuser_token_headers, json=body
        ).status_code
        == 200
    )
    monkeypatch.setattr(crud, "get_user_by_username", lambda **kwargs: None)
    conflict = client.post("/api/users/", headers=superuser_token_headers, json=body)
    assert conflict.status_code == 409
    assert (
        client.get("/api/users/me", headers=superuser_token_headers).status_code == 200
    )


def test_acceptance_browser_scope_usernames_fit_database_contract(db, monkeypatch):
    from cryptography.fernet import Fernet

    from app.core.config import settings
    from tests.acceptance.scenario import PASSWORD, Wire, seed_scope

    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )

    # Browser's second tenant label plus platform suffix must still fit 64.
    scope = seed_scope(
        db.get_bind(), Wire(), label="browser-1234567890-other", material_count=1
    )
    for username in (scope.username, scope.admin_username, scope.platform_username):
        assert len(username) <= 64
        assert UserCreate(username=username, password=PASSWORD).username == username
